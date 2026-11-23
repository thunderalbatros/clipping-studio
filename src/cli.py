"""Resumable Clipping Studio orchestrator CLI (`run`, `curate`, `trends`) backed by SQLite WAL/FTS5 (Hito 2.0)."""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from evals.media_check import evaluate_video
from src.curator import curate_run, generate_clip_packaging
from src.db import (
    get_daily_llm_spend_usd,
    get_scene_blueprint,
    init_db,
    insert_telemetry,
    inspect_db_summary,
    list_creators,
    list_scene_blueprints,
    seed_creators_from_yaml,
    upsert_clip,
    upsert_vod,
)
from src.miner import (
    PROJECT_ROOT,
    RUNS_DIR,
    auto_profile_creator,
    detect_platform,
    enforce_runs_quota,
    measure_runs_usage_mb,
    mine_vod_slices,
    probe_kick_channel,
    validate_run_id,
)
from src.renderer import render_ab_hook_variant, render_master_clip
from src.resource_guard import heavy_local_allowed
from src.schemas import ClipPackaging, RunState, SceneBlueprint
from src.transcriber import transcribe_run
from src.trends import run_trends_radar

TELEMETRY_PATH = RUNS_DIR / "telemetry.jsonl"


def _log_telemetry(run_id: str, event_type: str, event: dict[str, object]) -> None:
    TELEMETRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with TELEMETRY_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    insert_telemetry(run_id, event_type, event)


def _render_and_verify(run_dir: Path, state: RunState, ab_variants: int = 1) -> RunState:
    from src.analytics import emit_hitl_kit_5_networks
    from src.design_engine import render_editorial_cover_9_16

    compute_profile = os.getenv("COMPUTE_PROFILE", "local_hp").strip() or "local_hp"
    cand_map = {c.slice_id: c for c in state.candidates}
    tr_map = {t.slice_id: t for t in state.transcripts}
    critic_map = {c.clip_id: c for c in state.critic_results}
    rendered: list[str] = []
    packages: list[ClipPackaging] = []

    for plan in state.edit_plans:
        cand = cand_map[plan.slice_id]
        tr = tr_map[plan.slice_id]
        out_mp4 = run_dir / f"{plan.clip_id}_master_9_16.mp4"
        cover_clean_pre_ass = run_dir / f"{plan.clip_id}_cover_clean.jpg"
        bp_obj: SceneBlueprint | None = None
        if plan.scene_blueprint_key:
            cached_bp = get_scene_blueprint(plan.scene_blueprint_key)
            if cached_bp is not None:
                try:
                    bp_obj = SceneBlueprint.model_validate_json(str(cached_bp["blueprint_json"]))
                except Exception:
                    bp_obj = None
        render_master_clip(
            Path(cand.slice_video_path),
            plan,
            tr,
            out_mp4,
            compute_profile=compute_profile,
            clean_cover_out=cover_clean_pre_ass,
            enforce_speaker_guard=True,
            scene_blueprint=bp_obj,
        )

        qa_dir = run_dir / f"{plan.clip_id}_qa_frames"
        verdict = evaluate_video(out_mp4, qa_dir=qa_dir)
        verdict["layout_audit"] = {
            "layout_mode": plan.layout_mode,
            "webcam_upscale_factor": plan.webcam_upscale_factor,
            "no_webcam_gameplay_overlap": plan.no_webcam_gameplay_overlap,
            "gameplay_crop_px": plan.gameplay_crop_px,
            "source_width": cand.source_width,
            "source_height": cand.source_height,
            "source_fps": cand.source_fps,
        }
        (run_dir / f"{plan.clip_id}_media_check.json").write_text(json.dumps(verdict, indent=2), encoding="utf-8")
        rendered.append(str(out_mp4))

        lufs = float(verdict["checks"]["loudness_ebur128"].get("integrated_lufs") or 0.0)
        dbtp = float(verdict["checks"]["loudness_ebur128"].get("true_peak_dbtp") or 0.0)
        pkg_json_str = ""
        if verdict["passed"]:
            pkg = generate_clip_packaging(plan, tr, state.streamer_id)
            try:
                cover_9_16 = run_dir / f"{plan.clip_id}_cover_9_16.jpg"
                cover_headline = pkg.suggested_on_screen_hook or plan.hook_headline
                fallbacks = list(pkg.ab_hook_variants)
                if plan.freeze_frame is not None:
                    fallbacks.append(plan.freeze_frame.callout_text)
                # ALWAYS pass pre-libass clean 9:16 frame so ZERO burned captions leak into cover_9_16.jpg
                cover_source = cover_clean_pre_ass if cover_clean_pre_ass.is_file() else Path(cand.slice_video_path)
                cov_meta = render_editorial_cover_9_16(
                    video_or_img=cover_source,
                    headline=cover_headline,
                    out_jpg=cover_9_16,
                    clean_cover_jpg=cover_clean_pre_ass,
                    title=pkg.shorts_title,
                    fallback_headlines=fallbacks,
                )
                pkg.suggested_on_screen_hook = str(cov_meta.get("cover_headline_used") or pkg.suggested_on_screen_hook)
                pkg.cover_timestamp_ms = int(cov_meta.get("cover_timestamp_ms") or 0)
                pkg.cover_clean_path = str(cov_meta.get("cover_clean_path") or "")
                pkg.cover_9_16_path = str(cov_meta.get("cover_path") or "")
            except Exception as exc:
                print(f"[COVER_WARN] {plan.clip_id}: cover generation skipped ({exc})")

            if ab_variants >= 2:
                alt_hook = (pkg.ab_hook_variants[0] if pkg.ab_hook_variants else pkg.suggested_on_screen_hook) or plan.hook_headline
                var_b_mp4 = run_dir / f"{plan.clip_id}_variant_b_9_16.mp4"
                try:
                    render_ab_hook_variant(out_mp4, alt_hook, var_b_mp4, compute_profile=compute_profile)
                    rendered.append(str(var_b_mp4))
                except Exception as exc:
                    print(f"[AB_VARIANT_WARN] {plan.clip_id}: Variant B skipped ({exc})")

            packages.append(pkg)
            pkg_json_str = pkg.model_dump_json(indent=2)
            (run_dir / f"{plan.clip_id}_packaging.json").write_text(pkg_json_str, encoding="utf-8")
            hitl_kit = emit_hitl_kit_5_networks(pkg, out_mp4, run_dir)
            print(
                f"[PACKAGING] {plan.clip_id}: title={pkg.shorts_title!r} cover_hook={pkg.suggested_on_screen_hook!r} "
                f"tags={pkg.shorts_hashtags} cover_ts_ms={pkg.cover_timestamp_ms} kit={hitl_kit['kit_md_path']}"
            )

        cr = critic_map.get(plan.clip_id)
        upsert_clip(
            run_id=state.run_id,
            clip_id=plan.clip_id,
            slice_id=plan.slice_id,
            source_url=plan.source_url,
            streamer_id=state.streamer_id,
            start_trim=plan.start_trim,
            end_trim=plan.end_trim,
            layout_mode=plan.layout_mode,
            webcam_upscale_factor=plan.webcam_upscale_factor,
            no_webcam_gameplay_overlap=plan.no_webcam_gameplay_overlap,
            matched_lore_entity=plan.matched_lore_entity or "",
            critic_overall=cr.overall if cr else 0.0,
            media_check_passed=bool(verdict["passed"]),
            lufs=lufs,
            dbtp=dbtp,
            output_mp4=str(out_mp4),
            packaging_json=pkg_json_str,
        )
        print(
            f"[MEDIA_CHECK] {out_mp4.name}: passed={verdict['passed']} "
            f"lufs={lufs} dbtp={dbtp} upscale={plan.webcam_upscale_factor}x overlap_free={plan.no_webcam_gameplay_overlap}"
        )

    state.rendered_clips = rendered
    state.packaging = packages
    state.stage = "reviewed" if rendered else state.stage
    state.disk_usage_mb = enforce_runs_quota(RUNS_DIR, protect_dir=run_dir)
    state.updated_at = datetime.now(UTC).isoformat()
    (run_dir / "state.json").write_text(state.model_dump_json(indent=2), encoding="utf-8")
    upsert_vod(state.source_url, detect_platform(state.source_url), state.streamer_id, run_id=state.run_id, status=state.stage)
    return state


def cmd_run(args: argparse.Namespace) -> int:
    init_db()
    t0 = time.perf_counter()
    run_id = args.run_id or datetime.now(UTC).strftime("run_%Y%m%d_%H%M%S")
    run_dir = validate_run_id(run_id, RUNS_DIR)
    state_path = run_dir / "state.json"

    if args.probe_kick:
        kp = probe_kick_channel(args.probe_kick)
        print(f"[KICK_PROBE] {json.dumps(kp)}")

    stage_order = {"initialized": 0, "mined": 1, "transcribed": 2, "curated": 3, "rendered": 4, "reviewed": 5}
    cur_stage = 0
    if state_path.exists() and not args.fresh:
        prev = RunState.model_validate_json(state_path.read_text(encoding="utf-8"))
        cur_stage = stage_order.get(prev.stage, 0)

    if cur_stage < 1:
        state = mine_vod_slices(
            args.url,
            streamer_id=args.streamer,
            run_id=run_id,
            top_k=args.top_k,
            allow_short=args.allow_short,
            allow_trailer=args.allow_trailer,
            allow_720p=args.allow_720p,
            allow_unlisted=args.allow_unlisted,
        )
    else:
        state = RunState.model_validate_json(state_path.read_text(encoding="utf-8"))

    if cur_stage < 2:
        state = transcribe_run(run_dir, audio_policy=args.audio_policy)

    if cur_stage < 3:
        state = curate_run(
            run_dir,
            brief=args.brief,
            audio_policy=args.audio_policy,
            scene_research=bool(getattr(args, "scene_research", False)),
            game=str(getattr(args, "game", "GTA VI") or "GTA VI"),
        )

    if state.edit_plans:
        state = _render_and_verify(run_dir, state, ab_variants=args.ab_variants)
    else:
        print(f"[CLI] run_id={run_id} finished without approved clips (check slice_audits/critic_results).")

    elapsed = round(time.perf_counter() - t0, 2)
    _log_telemetry(
        run_id,
        "run_complete",
        {
            "ts": datetime.now(UTC).isoformat(),
            "run_id": run_id,
            "url": args.url,
            "streamer": args.streamer,
            "stage": state.stage,
            "approved_clips": len(state.edit_plans),
            "rendered_clips": len(state.rendered_clips),
            "disk_usage_mb": state.disk_usage_mb,
            "elapsed_sec": elapsed,
        },
    )
    print(f"[CLI] run_id={run_id} stage={state.stage} rendered={len(state.rendered_clips)} disk_mb={state.disk_usage_mb} elapsed_sec={elapsed}")
    return 0


def cmd_curate(args: argparse.Namespace) -> int:
    init_db()
    run_dir = validate_run_id(args.run_id, RUNS_DIR)
    if args.retranscribe:
        transcribe_run(run_dir, audio_policy=args.audio_policy)
    state = curate_run(
        run_dir,
        brief=args.brief,
        audio_policy=args.audio_policy,
        scene_research=bool(getattr(args, "scene_research", False)),
        game=str(getattr(args, "game", "GTA VI") or "GTA VI"),
    )
    if args.render and state.edit_plans:
        state = _render_and_verify(run_dir, state, ab_variants=args.ab_variants)
    print(f"[CLI_CURATE] run_id={args.run_id} audits={len(state.slice_audits)} plans={len(state.edit_plans)} audio_policy={state.audio_policy}")
    return 0


def cmd_trends(args: argparse.Namespace) -> int:
    init_db()
    payload = run_trends_radar(
        top_n=args.top_n,
        all_networks=not args.no_social,
        refresh_style_dna=bool(args.steal),
    )
    print(f"[TRENDS] sources={payload['sources']} top_count={len(payload['golden_references'])}")
    for idx, ref in enumerate(payload["golden_references"][:8], start=1):
        print(
            f"  #{idx} [{ref['platform']}] {ref['views_per_hour']} vph ({int(ref['duration_sec'])}s) "
            f"{ref['channel']}: {ref['title']}"
        )
    if "style_dna" in payload:
        print(f"[STYLE_DNA] samples={payload['style_dna']['sample_count']} targets={payload['style_dna']['recommended_targets']}")
    print(f"[SQLITE] {json.dumps(inspect_db_summary())}")
    return 0


def cmd_steal(args: argparse.Namespace) -> int:
    """Analyze one or more local MP4s or public viral URLs -> context/viral_style_dna.json."""
    from src.style_analyzer import analyze_video_style, download_and_analyze_url, synthesize_style_dna

    init_db()
    samples: list[dict[str, object]] = []
    for target in args.inputs:
        p = Path(target)
        if p.exists():
            samples.append(analyze_video_style(p))
        elif target.startswith(("http://", "https://")):
            dna = download_and_analyze_url(target, max_sec=args.max_sec)
            if dna:
                samples.append(dna)
    payload = synthesize_style_dna(samples)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def cmd_bounties(args: argparse.Namespace) -> int:
    """Scan, import (HITL), purge junk, list, or approve CPM bounty campaigns."""
    from src.bounties import approve_bounty, get_bounties, import_bounty_hitl, purge_junk_campaigns, scan_bounties

    init_db()
    if args.bounty_cmd == "scan":
        found = scan_bounties(max_per_platform=args.limit, enrich_firecrawl=bool(args.firecrawl))
        print(json.dumps({"discovered": len(found), "campaigns": found}, indent=2, ensure_ascii=False))
        return 0
    if args.bounty_cmd == "import":
        rec = import_bounty_hitl(
            platform=args.platform,
            title=args.title,
            url=args.url,
            cpm_usd=args.cpm,
            burn_rate_pct=args.burn_rate,
            geo_requirement=args.geo,
            platforms=args.platforms,
            audio_policy=args.audio_policy,
            brief_text=args.brief,
            voice_policy=args.voice_policy,
            verdict=args.verdict,
        )
        print(json.dumps(rec, indent=2, ensure_ascii=False))
        return 0
    if args.bounty_cmd == "purge-junk":
        res = purge_junk_campaigns()
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0
    if args.bounty_cmd == "list":
        rows = get_bounties(verdict=args.verdict)
        print(json.dumps({"count": len(rows), "campaigns": rows}, indent=2, ensure_ascii=False))
        return 0
    if args.bounty_cmd == "approve":
        ok = approve_bounty(args.id, verdict=args.verdict)
        print(json.dumps({"campaign_id": args.id, "verdict": args.verdict, "updated": ok}))
        return 0 if ok else 1
    return 1


def cmd_gc(args: argparse.Namespace) -> int:
    """Purge temporary/intermediate files inside `runs/` while preserving studio.db, models/, and READY_HITL clips."""
    from src.gc import run_storage_gc

    init_db()
    report = run_storage_gc(ttl_hours=args.ttl_hours, dry_run=bool(args.dry_run))
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def cmd_snipe(args: argparse.Namespace) -> int:
    """Run pure-Python Live-Stream Sniper (Twitch IRC + Kick REST/HLS + Hawkes + 90s ring-buffer)."""
    import asyncio

    from src.live_sniper import run_live_sniper

    init_db()
    channels = [c.strip() for c in args.channels.split(",") if c.strip()]
    summary = asyncio.run(
        run_live_sniper(
            channels,
            duration_sec=args.duration,
            post_roll_sec=args.post_roll,
            enable_hls=not args.no_video,
            auto_render=bool(args.auto_render),
            dry_run=bool(args.dry_run),
        )
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


def cmd_compile(args: argparse.Namespace) -> int:
    """Compile approved clips into a 16:9 1920x1080 YouTube Long-Form master + chapters + 1280x720 thumbnail."""
    from src.compiler import compile_from_studio_db, compile_longform_16_9

    init_db()
    out_dir = Path(args.out_dir)
    thumb_src = Path(args.thumb_source) if args.thumb_source else None
    if args.clips:
        specs = [{"path": c, "label": f"Moment #{i}"} for i, c in enumerate(args.clips, start=1)]
        report = compile_longform_16_9(
            specs,
            out_dir=out_dir,
            title=args.title,
            thumbnail_headline=args.thumb_headline,
            thumb_source=thumb_src,
        )
    else:
        report = compile_from_studio_db(
            out_dir=out_dir,
            streamer_id=args.streamer,
            max_clips=args.max_clips,
            title=args.title,
            thumbnail_headline=args.thumb_headline,
            thumb_source=thumb_src,
        )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def cmd_cover(args: argparse.Namespace) -> int:
    """Standalone Method-C 9:16 editorial cover or native 16:9 YouTube thumbnail generator."""
    from src.design_engine import render_editorial_cover_9_16, render_editorial_thumbnail_16_9

    src_path = Path(args.video)
    if not src_path.exists():
        raise FileNotFoundError(f"Video or image not found: {src_path}")

    webcam_box: tuple[int, int, int, int] | None = None
    if args.webcam_box:
        parts = [int(v.strip()) for v in args.webcam_box.split(",")]
        if len(parts) != 4:
            raise ValueError("--webcam-box must be 'x,y,w,h'")
        webcam_box = (parts[0], parts[1], parts[2], parts[3])

    if args.format == "16_9":
        out_jpg = Path(args.out) if args.out else src_path.with_name("thumbnail_1280x720.jpg")
        report = render_editorial_thumbnail_16_9(
            video_or_img=src_path,
            headline=args.headline,
            out_jpg=out_jpg,
            badge_text=args.badge,
            webcam_box_px=webcam_box,
        )
    else:
        out_jpg = Path(args.out) if args.out else src_path.with_name(f"{src_path.stem}_cover_9_16.jpg")
        report = render_editorial_cover_9_16(
            video_or_img=src_path,
            headline=args.headline,
            out_jpg=out_jpg,
            badge_text=args.badge,
            webcam_box_px=webcam_box,
        )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def cmd_analytics(args: argparse.Namespace) -> int:
    """Record post-publish metrics or summarize multi-network performance feedback."""
    from src.analytics import record_publish_metrics, summarize_performance

    init_db()
    if args.analytics_cmd == "log":
        validate_run_id(args.run_id, RUNS_DIR)
        rec = record_publish_metrics(
            run_id=args.run_id,
            clip_id=args.clip_id,
            platform=args.platform,
            views=args.views,
            likes=args.likes,
            shares=args.shares,
            comments=args.comments,
            avg_view_pct=args.avg_view_pct,
            swiped_away_pct=args.swiped_away_pct,
            vvsa_pct=args.vvsa_pct,
            sends_per_reach=args.sends_per_reach,
            cpm_usd=args.cpm_usd,
            published_url=args.url,
            streamer_id=args.streamer,
            hook_style=args.hook_style,
        )
        print(json.dumps(rec, indent=2, ensure_ascii=False))
        return 0
    report = summarize_performance()
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def cmd_rag(args: argparse.Namespace) -> int:
    """Rebuild or query the hybrid local RAG (FTS5 + sqlite-vec + fastembed) & read-only Omni-RAG v6 bridge."""
    from src.rag_engine import format_rag_context, query_rag, rebuild_rag_index

    init_db()
    if args.rag_cmd == "rebuild":
        stats = rebuild_rag_index(skip_vectors=bool(args.no_vectors))
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        return 0
    if args.rag_cmd == "query":
        hits = query_rag(
            args.text,
            domain=args.domain,
            top_k=args.top_k,
            federated=bool(args.federated),
            use_vectors=not bool(args.bm25_only),
        )
        payload = {
            "query": args.text,
            "domain": args.domain,
            "federated": bool(args.federated),
            "hit_count": len(hits),
            "hits": hits,
            "formatted_context": format_rag_context(hits, max_tokens=args.max_tokens),
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    return 1


def cmd_research(args: argparse.Namespace) -> int:
    """Run 2-Level Scene-Match Intelligence (YouTube Shorts + Kick Clips -> SQLite scene_blueprints)."""
    from src.scene_researcher import research_scene

    init_db()
    if args.list_cache:
        rows = list_scene_blueprints(game=args.game if args.game_filter else None, limit=args.limit)
        print(json.dumps({"count": len(rows), "blueprints": rows}, indent=2, ensure_ascii=False))
        return 0
    if not args.query:
        raise SystemExit("--query is required unless --list-cache is set")
    bp = research_scene(
        scene_query=args.query,
        game=args.game,
        lore_entity=args.lore,
        max_refs=args.max_refs,
        force_refresh=bool(args.force),
        use_multimodal=not bool(args.no_multimodal),
    )
    print(bp.model_dump_json(indent=2))
    return 0


def cmd_creators(args: argparse.Namespace) -> int:
    """Manage dynamic `creators` table (seed from streamers.yaml, list, or auto-profile new client/bounty creator)."""
    init_db()
    if args.creators_cmd == "seed":
        res = seed_creators_from_yaml()
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0
    if args.creators_cmd == "list":
        rows = list_creators(tier=args.tier)
        print(json.dumps({"count": len(rows), "creators": rows}, indent=2, ensure_ascii=False))
        return 0
    if args.creators_cmd == "profile":
        rec = auto_profile_creator(
            args.creator_id,
            source_url=args.url,
            tier=args.tier,
            language=args.language,
            webcam_style=args.webcam_style,
            style_notes=args.notes,
            persist=True,
        )
        print(json.dumps(rec, indent=2, ensure_ascii=False))
        return 0
    return 1


def cmd_doctor(_args: argparse.Namespace) -> int:
    """Audit environment, hardware headroom, assets, ONNX models, SQLite WAL, and secret presence (zero values)."""
    import shutil

    import onnxruntime as ort
    from dotenv import load_dotenv

    from src.design_engine import REMBG_HOME, find_headless_browser
    from src.miner import _ensure_media_path, find_deno_executable
    from src.rag_engine import inspect_rag_status
    from src.renderer import find_auto_editor_executable

    _ensure_media_path()
    load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=True)
    allowed, reason, snap = heavy_local_allowed()
    runs_mb = measure_runs_usage_mb(RUNS_DIR)
    db_info = inspect_db_summary()
    daily_usd = get_daily_llm_spend_usd()
    daily_cap = float(os.getenv("LLM_DAILY_BUDGET_USD", "2.50") or 2.50)

    env_keys = (
        "GCP_PROJECT_ID",
        "GROQ_API_KEY",
        "OPENROUTER_API_KEY",
        "YOUTUBE_API_KEY",
        "SERPER_API_KEY",
        "FIRECRAWL_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    )
    env_status = {k: ("PRESENTE" if os.getenv(k, "").strip() else "FALTANTE") for k in env_keys}

    deno_exe = find_deno_executable()
    deno_found = deno_exe is not None
    browser_bin = find_headless_browser()
    bins = {
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ffprobe": bool(shutil.which("ffprobe")),
        "deno": deno_found,
        "deno_path": str(deno_exe) if deno_exe is not None else None,
        "yt-dlp": (PROJECT_ROOT / ".venv" / "Scripts" / "yt-dlp.exe").exists() or bool(shutil.which("yt-dlp")),
        "TwitchDownloaderCLI": (PROJECT_ROOT / ".venv" / "Scripts" / "TwitchDownloaderCLI.exe").exists()
        or bool(shutil.which("TwitchDownloaderCLI")),
        "auto-editor": find_auto_editor_executable() is not None,
        "rtk": bool(shutil.which("rtk")),
        "headless_browser": browser_bin.name if browser_bin else None,
    }
    birefnet_files = list(REMBG_HOME.rglob("*birefnet-general-lite*.onnx")) if REMBG_HOME.exists() else []
    assets = {
        "montserrat_ttf": (PROJECT_ROOT / "assets" / "fonts" / "Montserrat-Black.ttf").exists(),
        "montserrat_ofl": (PROJECT_ROOT / "assets" / "fonts" / "OFL.txt").exists(),
        "sfx_count": len(list((PROJECT_ROOT / "assets" / "sfx").glob("*.wav"))),
        "ultraface_onnx": (PROJECT_ROOT / "models" / "ultraface-rfb-320.onnx").exists(),
        "lr_asd_frontend": (PROJECT_ROOT / "models" / "lr-asd-frontend.onnx").exists(),
        "lr_asd_backend": (PROJECT_ROOT / "models" / "lr-asd-backend.onnx").exists(),
        "yamnet_onnx": (PROJECT_ROOT / "models" / "yamnet" / "yamnet.onnx").exists(),
        "birefnet_general_lite_onnx": bool(birefnet_files),
        "rtk_hooks_json": (PROJECT_ROOT / ".agents" / "hooks.json").is_file(),
    }
    report = {
        "hardware": snap,
        "heavy_local_allowed": {"allowed": allowed, "reason": reason},
        "storage": {"runs_mb": runs_mb, "quota_mb": 4096.0},
        "llm_budget_utc_day": {"spent_usd": daily_usd, "cap_usd": daily_cap},
        "onnxruntime_version": ort.__version__,
        "sqlite": db_info,
        "rag": inspect_rag_status(),
        "binaries": bins,
        "assets": assets,
        "env_keys": env_status,
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Clipping Studio Orchestrator CLI (v3.1)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Execute resumable end-to-end 1080p pipeline")
    p_run.add_argument("--url", required=True)
    p_run.add_argument("--streamer", default="xqc")
    p_run.add_argument("--run-id", default=None)
    p_run.add_argument("--top-k", type=int, default=2)
    p_run.add_argument("--brief", default=None, help="Optional campaign brief matched against gta6_lore_index.json")
    p_run.add_argument(
        "--audio-policy",
        choices=["strict_clean", "sound_campaign", "keep_original"],
        default="strict_clean",
        help="3-mode YAMNet audio policy (strict_clean | sound_campaign | keep_original)",
    )
    p_run.add_argument("--ab-variants", type=int, default=1, help="Set to 2 to also render Variant B (IG Trial Reels)")
    p_run.add_argument("--scene-research", action="store_true", help="Run Scene-Match Intelligence before Pass-2 editing")
    p_run.add_argument("--game", default="GTA VI", help="Game/niche context for Scene-Match Intelligence")
    p_run.add_argument("--allow-short", action="store_true")
    p_run.add_argument("--allow-trailer", action="store_true")
    p_run.add_argument("--allow-720p", action="store_true")
    p_run.add_argument("--allow-unlisted", action="store_true")
    p_run.add_argument("--probe-kick", default=None)
    p_run.add_argument("--fresh", action="store_true")
    p_run.set_defaults(func=cmd_run)

    p_cur = sub.add_parser("curate", help="Re-run transcriber/curator/render on an existing run")
    p_cur.add_argument("--run-id", required=True)
    p_cur.add_argument("--brief", default=None)
    p_cur.add_argument(
        "--audio-policy",
        choices=["strict_clean", "sound_campaign", "keep_original"],
        default="strict_clean",
    )
    p_cur.add_argument("--ab-variants", type=int, default=1)
    p_cur.add_argument("--scene-research", action="store_true")
    p_cur.add_argument("--game", default="GTA VI")
    p_cur.add_argument("--retranscribe", action="store_true")
    p_cur.add_argument("--render", action="store_true")
    p_cur.set_defaults(func=cmd_curate)

    p_tr = sub.add_parser("trends", help="Refresh golden_references.json across 5 networks + Kick")
    p_tr.add_argument("--top-n", type=int, default=10)
    p_tr.add_argument("--no-social", action="store_true", help="Skip TikTok/IG/FB/X Serper outlier discovery")
    p_tr.add_argument("--steal", action="store_true", help="Also refresh context/viral_style_dna.json via FFmpeg")
    p_tr.set_defaults(func=cmd_trends)

    p_st = sub.add_parser("steal", help="Extract empirical Viral Style DNA (cuts/min, opening LUFS) from MP4s or URLs")
    p_st.add_argument("inputs", nargs="+", help="Local MP4 file paths or public video URLs")
    p_st.add_argument("--max-sec", type=int, default=45)
    p_st.set_defaults(func=cmd_steal)

    p_bn = sub.add_parser(
        "bounties",
        aliases=["bounty"],
        help="Bounty Radar & HITL CPM campaign auditor (Whop/Clipping.net/Vyro/ClipBoom)",
    )
    bn_sub = p_bn.add_subparsers(dest="bounty_cmd", required=True)
    bn_scan = bn_sub.add_parser("scan", help="Scan public bounty directories -> SQLite (PENDING_HITL)")
    bn_scan.add_argument("--limit", type=int, default=3)
    bn_scan.add_argument("--firecrawl", action="store_true", help="Enrich URLs with Firecrawl markdown")
    bn_imp = bn_sub.add_parser("import", help="Import a verified HITL campaign brief/URL into runs/studio.db")
    bn_imp.add_argument("--platform", required=True, help="whop | clipping_net | vyro | clipboom | direct")
    bn_imp.add_argument("--title", required=True)
    bn_imp.add_argument("--url", required=True)
    bn_imp.add_argument("--cpm", type=float, required=True, help="CPM USD per 1k views (must be > 0)")
    bn_imp.add_argument("--burn-rate", type=float, default=35.0)
    bn_imp.add_argument("--geo", choices=["global", "tier1_60pct", "latam"], default="global")
    bn_imp.add_argument("--platforms", default="youtube_shorts,x")
    bn_imp.add_argument(
        "--audio-policy",
        choices=["strict_clean", "sound_campaign", "keep_original"],
        default="strict_clean",
    )
    bn_imp.add_argument("--voice-policy", default="none")
    bn_imp.add_argument("--brief", default="")
    bn_imp.add_argument("--verdict", choices=["PENDING_HITL", "APPROVED", "REJECTED"], default="PENDING_HITL")
    bn_sub.add_parser("purge-junk", help="Remove invalid non-campaign rows (cpm<=0, blogs, stores) from studio.db")
    bn_list = bn_sub.add_parser("list", help="List persisted campaigns in runs/studio.db (Argentina geo-prioritized)")
    bn_list.add_argument("--verdict", default=None, choices=["PENDING_HITL", "APPROVED", "REJECTED"])
    bn_app = bn_sub.add_parser("approve", help="Apply human-in-the-loop verdict to a campaign")
    bn_app.add_argument("--id", required=True)
    bn_app.add_argument("--verdict", default="APPROVED", choices=["APPROVED", "REJECTED", "PENDING_HITL"])
    p_bn.set_defaults(func=cmd_bounties)

    p_gc = sub.add_parser("gc", help="Purge disposable intermediate files in runs/ preserving studio.db, models/ and READY_HITL")
    p_gc.add_argument("--dry-run", action="store_true", help="Report reclaimable files without deleting")
    p_gc.add_argument("--ttl-hours", type=float, default=72.0, help="Age threshold in hours for intermediate files")
    p_gc.set_defaults(func=cmd_gc)

    p_sn = sub.add_parser("snipe", help="Monitor 1-2 live Twitch/Kick streams for chat spikes -> JIT 90s clip")
    p_sn.add_argument("--channels", required=True, help="Comma-separated channels, e.g. twitch:caseoh_,kick:xqc")
    p_sn.add_argument("--duration", type=float, default=120.0, help="Monitor duration in seconds")
    p_sn.add_argument("--post-roll", type=float, default=20.0, help="Seconds to wait after spike before flush")
    p_sn.add_argument("--no-video", action="store_true", help="Chat-only monitoring (skip HLS ring-buffer)")
    p_sn.add_argument("--auto-render", action="store_true", help="Automatically run full pipeline on captured spike")
    p_sn.add_argument("--dry-run", action="store_true", help="Detect spikes without writing MP4s")
    p_sn.set_defaults(func=cmd_snipe)

    p_cp = sub.add_parser("compile", help="Compile approved clips into 16:9 1920x1080 YouTube Long-Form + thumbnail")
    p_cp.add_argument("--out-dir", default="runs/compilation_01")
    p_cp.add_argument("--clips", nargs="*", default=None, help="Explicit MP4 paths (defaults to approved clips in studio.db)")
    p_cp.add_argument("--streamer", default=None)
    p_cp.add_argument("--max-clips", type=int, default=12)
    p_cp.add_argument("--title", default="Best GTA VI Moments Compilation")
    p_cp.add_argument("--thumb-headline", default="GTA VI CHAOS")
    p_cp.add_argument("--thumb-source", default=None, help="Explicit native 16:9 slice MP4 for thumbnail generation")
    p_cp.set_defaults(func=cmd_compile)

    p_cov = sub.add_parser("cover", help="Generate Method-C 9:16 editorial cover or native 16:9 YouTube thumbnail + QA artifacts")
    p_cov.add_argument("--video", required=True, help="Source MP4 or image path")
    p_cov.add_argument("--headline", required=True, help="2-4 word Curiosity-Gap headline")
    p_cov.add_argument("--format", choices=["9_16", "16_9"], default="9_16")
    p_cov.add_argument("--out", default=None, help="Optional output JPG path")
    p_cov.add_argument("--badge", default="GTA VI", help="Optional micro-eyebrow badge text")
    p_cov.add_argument("--webcam-box", default=None, help="Optional 'x,y,w,h' webcam pixel ROI on 16:9 slice")
    p_cov.set_defaults(func=cmd_cover)

    p_an = sub.add_parser("analytics", help="Post-publish 5-network metrics & performance feedback loop")
    an_sub = p_an.add_subparsers(dest="analytics_cmd", required=True)
    an_log = an_sub.add_parser("log", help="Record views/likes/shares/retention for a published clip")
    an_log.add_argument("--run-id", required=True)
    an_log.add_argument("--clip-id", required=True)
    an_log.add_argument("--platform", required=True)
    an_log.add_argument("--views", type=int, required=True)
    an_log.add_argument("--likes", type=int, default=0)
    an_log.add_argument("--shares", type=int, default=0)
    an_log.add_argument("--comments", type=int, default=0)
    an_log.add_argument("--avg-view-pct", type=float, default=0.0)
    an_log.add_argument("--swiped-away-pct", type=float, default=0.0)
    an_log.add_argument("--vvsa-pct", type=float, default=None)
    an_log.add_argument("--sends-per-reach", type=float, default=0.0)
    an_log.add_argument("--cpm-usd", type=float, default=0.0)
    an_log.add_argument("--url", default="")
    an_log.add_argument("--streamer", default="")
    an_log.add_argument("--hook-style", default="")
    an_sub.add_parser("summary", help="Summarize multi-network performance -> context/performance_feedback.json")
    p_an.set_defaults(func=cmd_analytics)

    p_rag = sub.add_parser("rag", help="Rebuild or query hybrid local RAG (FTS5+sqlite-vec) & read-only Omni-RAG v6")
    rag_sub = p_rag.add_subparsers(dest="rag_cmd", required=True)
    rag_reb = rag_sub.add_parser("rebuild", help="Index all 6 knowledge gaps + episodic memory into runs/studio.db")
    rag_reb.add_argument("--no-vectors", action="store_true", help="Rebuild FTS5 only without computing embeddings")
    rag_q = rag_sub.add_parser("query", help="Run hybrid BM25+vec RRF query (optionally federated to Omni-RAG v6)")
    rag_q.add_argument("text", help="Natural-language search query")
    rag_q.add_argument("--domain", default=None, help="Filter local domain (design_ctr, gta6_streamers, episodic_memory, ...)")
    rag_q.add_argument("--top-k", type=int, default=5)
    rag_q.add_argument("--federated", action="store_true", help="Also query Omni-RAG v6 strictly in ?mode=ro&immutable=1")
    rag_q.add_argument("--bm25-only", action="store_true", help="Skip vector query leg")
    rag_q.add_argument("--max-tokens", type=int, default=550)
    p_rag.set_defaults(func=cmd_rag)

    p_res = sub.add_parser("research", help="Scene-Match Intelligence: research viral references for an exact scene")
    p_res.add_argument("--query", default="", help="Exact scene/mission description (e.g. 'Destroy Donnie car gas can explosion')")
    p_res.add_argument("--game", default="GTA VI", help="Game, niche, or campaign context (e.g. 'GTA VI', 'GTA IV', 'podcast')")
    p_res.add_argument("--lore", default=None, help="Optional lore entity or character name")
    p_res.add_argument("--max-refs", type=int, default=3, help="Max viral references to deconstruct (3-5)")
    p_res.add_argument("--force", action="store_true", help="Bypass SQLite scene_blueprints cache")
    p_res.add_argument("--no-multimodal", action="store_true", help="Skip Gemini 3.8 Flash contact-strip audit")
    p_res.add_argument("--list-cache", action="store_true", help="List cached blueprints in runs/studio.db")
    p_res.add_argument("--game-filter", action="store_true", help="Filter --list-cache by --game")
    p_res.add_argument("--limit", type=int, default=20)
    p_res.set_defaults(func=cmd_research)

    p_cr = sub.add_parser("creators", help="Manage persistent `creators` table (radar_seed | bounty_target | client)")
    cr_sub = p_cr.add_subparsers(dest="creators_cmd", required=True)
    cr_sub.add_parser("seed", help="Idempotently seed creators from config/streamers.yaml as radar_seed")
    cr_list = cr_sub.add_parser("list", help="List persisted creator profiles")
    cr_list.add_argument("--tier", default=None, choices=["radar_seed", "bounty_target", "client"])
    cr_prof = cr_sub.add_parser("profile", help="Run 1-time Auto-Profiler for a new bounty_target or client creator")
    cr_prof.add_argument("--creator-id", required=True)
    cr_prof.add_argument("--url", default="")
    cr_prof.add_argument("--tier", default="bounty_target", choices=["radar_seed", "bounty_target", "client"])
    cr_prof.add_argument("--language", default="en")
    cr_prof.add_argument("--webcam-style", default="auto_detect")
    cr_prof.add_argument("--notes", default="")
    p_cr.set_defaults(func=cmd_creators)

    p_doc = sub.add_parser("doctor", help="Audit hardware, binaries, ONNX models, SQLite WAL, and env key presence")
    p_doc.set_defaults(func=cmd_doctor)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
