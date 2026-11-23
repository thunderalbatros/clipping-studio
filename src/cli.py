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
    init_db,
    insert_telemetry,
    inspect_db_summary,
    upsert_clip,
    upsert_vod,
)
from src.miner import (
    PROJECT_ROOT,
    RUNS_DIR,
    detect_platform,
    enforce_runs_quota,
    mine_vod_slices,
    probe_kick_channel,
)
from src.renderer import render_master_clip
from src.resource_guard import heavy_local_allowed
from src.schemas import ClipPackaging, RunState
from src.transcriber import transcribe_run
from src.trends import run_trends_radar

TELEMETRY_PATH = RUNS_DIR / "telemetry.jsonl"


def _log_telemetry(run_id: str, event_type: str, event: dict[str, object]) -> None:
    TELEMETRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with TELEMETRY_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    insert_telemetry(run_id, event_type, event)


def _render_and_verify(run_dir: Path, state: RunState) -> RunState:
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
        render_master_clip(Path(cand.slice_video_path), plan, tr, out_mp4, compute_profile=compute_profile)

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
            packages.append(pkg)
            pkg_json_str = pkg.model_dump_json(indent=2)
            (run_dir / f"{plan.clip_id}_packaging.json").write_text(pkg_json_str, encoding="utf-8")
            print(f"[PACKAGING] {plan.clip_id}: title={pkg.shorts_title!r} tags={pkg.shorts_hashtags}")

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
    run_dir = RUNS_DIR / run_id
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
        state = transcribe_run(run_dir)

    if cur_stage < 3:
        state = curate_run(run_dir, brief=args.brief)

    if state.edit_plans:
        state = _render_and_verify(run_dir, state)
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
    run_dir = RUNS_DIR / args.run_id
    if args.retranscribe:
        transcribe_run(run_dir)
    state = curate_run(run_dir, brief=args.brief)
    if args.render and state.edit_plans:
        state = _render_and_verify(run_dir, state)
    print(f"[CLI_CURATE] run_id={args.run_id} audits={len(state.slice_audits)} plans={len(state.edit_plans)}")
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
    """Scan, list, or apply HITL approval verdict to CPM bounty campaigns."""
    from src.bounties import approve_bounty, get_bounties, scan_bounties

    init_db()
    if args.bounty_cmd == "scan":
        found = scan_bounties(max_per_platform=args.limit, enrich_firecrawl=bool(args.firecrawl))
        print(json.dumps({"discovered": len(found), "campaigns": found}, indent=2, ensure_ascii=False))
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
    if args.clips:
        specs = [{"path": c, "label": f"Moment #{i}"} for i, c in enumerate(args.clips, start=1)]
        report = compile_longform_16_9(
            specs,
            out_dir=out_dir,
            title=args.title,
            thumbnail_headline=args.thumb_headline,
        )
    else:
        report = compile_from_studio_db(
            out_dir=out_dir,
            streamer_id=args.streamer,
            max_clips=args.max_clips,
            title=args.title,
        )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def cmd_analytics(args: argparse.Namespace) -> int:
    """Record post-publish metrics or summarize multi-network performance feedback."""
    from src.analytics import record_publish_metrics, summarize_performance

    init_db()
    if args.analytics_cmd == "log":
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
            published_url=args.url,
            streamer_id=args.streamer,
            hook_style=args.hook_style,
        )
        print(json.dumps(rec, indent=2, ensure_ascii=False))
        return 0
    report = summarize_performance()
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    """Audit environment, hardware headroom, assets, ONNX models, SQLite WAL, and secret presence (zero values)."""
    import shutil

    from dotenv import load_dotenv

    from src.miner import _ensure_media_path

    _ensure_media_path()
    load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=True)
    allowed, reason, snap = heavy_local_allowed()
    runs_mb = enforce_runs_quota(RUNS_DIR)
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

    deno_found = (
        bool(shutil.which("deno"))
        or (Path.home() / ".deno" / "bin" / "deno.exe").exists()
        or (Path(os.getenv("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "deno.exe").exists()
    )
    bins = {
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ffprobe": bool(shutil.which("ffprobe")),
        "deno": deno_found,
        "yt-dlp": (PROJECT_ROOT / ".venv" / "Scripts" / "yt-dlp.exe").exists() or bool(shutil.which("yt-dlp")),
        "TwitchDownloaderCLI": (PROJECT_ROOT / ".venv" / "Scripts" / "TwitchDownloaderCLI.exe").exists()
        or bool(shutil.which("TwitchDownloaderCLI")),
    }
    assets = {
        "montserrat_ttf": (PROJECT_ROOT / "assets" / "fonts" / "Montserrat-Black.ttf").exists(),
        "montserrat_ofl": (PROJECT_ROOT / "assets" / "fonts" / "OFL.txt").exists(),
        "sfx_count": len(list((PROJECT_ROOT / "assets" / "sfx").glob("*.wav"))),
        "ultraface_onnx": (PROJECT_ROOT / "models" / "ultraface-rfb-320.onnx").exists(),
        "lr_asd_frontend": (PROJECT_ROOT / "models" / "lr-asd-frontend.onnx").exists(),
        "lr_asd_backend": (PROJECT_ROOT / "models" / "lr-asd-backend.onnx").exists(),
        "yamnet_onnx": (PROJECT_ROOT / "models" / "yamnet" / "yamnet.onnx").exists(),
    }
    report = {
        "hardware": snap,
        "heavy_local_allowed": {"allowed": allowed, "reason": reason},
        "storage": {"runs_mb": runs_mb, "quota_mb": 4096.0},
        "llm_budget_utc_day": {"spent_usd": daily_usd, "cap_usd": daily_cap},
        "sqlite": db_info,
        "binaries": bins,
        "assets": assets,
        "env_keys": env_status,
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Clipping Studio Orchestrator CLI (v2.5)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Execute resumable end-to-end 1080p pipeline")
    p_run.add_argument("--url", required=True)
    p_run.add_argument("--streamer", default="xqc")
    p_run.add_argument("--run-id", default=None)
    p_run.add_argument("--top-k", type=int, default=2)
    p_run.add_argument("--brief", default=None, help="Optional campaign brief matched against gta6_lore_index.json")
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

    p_bn = sub.add_parser("bounties", help="Bounty Radar & HITL CPM campaign auditor (Whop/Clipping.net/Vyro/ClipBoom)")
    bn_sub = p_bn.add_subparsers(dest="bounty_cmd", required=True)
    bn_scan = bn_sub.add_parser("scan", help="Scan public bounty directories -> SQLite (PENDING_HITL)")
    bn_scan.add_argument("--limit", type=int, default=3)
    bn_scan.add_argument("--firecrawl", action="store_true", help="Enrich URLs with Firecrawl markdown")
    bn_list = bn_sub.add_parser("list", help="List persisted campaigns in runs/studio.db")
    bn_list.add_argument("--verdict", default=None, choices=["PENDING_HITL", "APPROVED", "REJECTED"])
    bn_app = bn_sub.add_parser("approve", help="Apply human-in-the-loop verdict to a campaign")
    bn_app.add_argument("--id", required=True)
    bn_app.add_argument("--verdict", default="APPROVED", choices=["APPROVED", "REJECTED", "PENDING_HITL"])
    p_bn.set_defaults(func=cmd_bounties)

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
    p_cp.set_defaults(func=cmd_compile)

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
    an_log.add_argument("--url", default="")
    an_log.add_argument("--streamer", default="")
    an_log.add_argument("--hook-style", default="")
    an_sub.add_parser("summary", help="Summarize multi-network performance -> context/performance_feedback.json")
    p_an.set_defaults(func=cmd_analytics)

    p_doc = sub.add_parser("doctor", help="Audit hardware, binaries, ONNX models, SQLite WAL, and env key presence")
    p_doc.set_defaults(func=cmd_doctor)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
