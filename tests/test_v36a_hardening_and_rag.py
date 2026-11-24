"""Comprehensive v3.6-A unit & integration test suite.

Covers:
1. `src/llm_router.py`: zero `gemini-2.5-flash` references, `media_resolution` -> `GoogleModelSettings`, Flex/Batch `-50%` cost & headers.
2. `src/runtime_paths.py`: cross-platform binary resolution, `--no-sandbox --disable-dev-shm-usage`, bundled font, `format_hms("60" -> "00:01:00")`, `parse_iso_duration_sec`, `OMNI_RAG_CACHE_DIR`.
3. `src/resource_guard.py`: `psutil` snapshot + `unload_heavy_models()` evicting `_MODEL_CACHE`, `_SESSIONS`, `_REMBG_SESSIONS`, `_EMBEDDER_CACHE`, `_RERANKER_CACHE`.
4. `src/rag_engine.py`: atomic JSON chunks without `[:1600]` truncation, `7/7` skills indexed, `knowledge_chunks` dropped, `sqlite-vec` native `domain` metadata pre-KNN filter, `TextCrossEncoder` reranking + GTA VI anti-noise guard.
5. Security hardening: `src/bounties.py` SSRF (`safehttpx` + DNS/IP guard + `official sound` regex), `src/notifier.py` `html.escape`, `src/live_sniper.py` IRC channel regex + HLS 403 refresh, `scripts/security_pretool_hook.py` symmetric `read_file` & `python -c` guard.
6. Media, Packaging & Evals: `evals/media_check.py` (`UNIVERSAL_SAFE_BOX_9_16` `y1=420` + CFR check), `evals/visual_qa.py` (`pydantic-evals` pass & fail cases), `src/design_engine.py` (`clean_alpha_floating_islands` + `cover != overlay`), `src/speaker_guard.py` MFCC, `src/trends.py` hook classifier, `src/gc.py` `runs/branding/` protection + closed-run `slice_*.mp4` cleanup, `src/compiler.py` native `16:9` slice resolution.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from google.genai.types import MediaResolution
from PIL import Image, ImageDraw

from evals.media_check import check_ffprobe_spec, draw_safezones_overlay, evaluate_video
from evals.visual_qa import (
    VisualQAInputs,
    VisualQAVlmVerdict,
    evaluate_visual_qa,
    is_cover_overlay_duplicated,
)
from scripts.security_pretool_hook import evaluate_security_payload
from src.bounties import audit_campaign_rules, is_valid_campaign_record, validate_public_http_url
from src.compiler import compile_from_studio_db
from src.db import init_db, inspect_db_summary, upsert_clip, upsert_creator, upsert_slice
from src.design_engine import clean_alpha_floating_islands, ensure_complementary_cover_headline
from src.gc import run_storage_gc
from src.live_sniper import HLSRingBuffer, _poll_hls_ring_buffer, validate_channel_login
from src.llm_router import (
    _estimate_vertex_cost_usd,
    _vertex_candidates,
    build_google_model_settings,
)
from src.notifier import send_bounty_alert, send_clip_ready_alert
from src.rag_engine import (
    _is_webdev_noise_hit,
    _passes_domain_guard,
    collect_all_knowledge_chunks,
    query_rag,
    rebuild_rag_index,
)
from src.renderer import map_output_ts_to_source_offset
from src.resource_guard import heavy_local_allowed, snapshot, unload_heavy_models
from src.runtime_paths import (
    PROJECT_ROOT,
    dir_size_bytes,
    format_hms,
    headless_browser_flags,
    load_bundled_font,
    parse_iso_duration_sec,
    resolve_binary,
    resolve_omni_rag_dir,
)
from src.schemas import UNIVERSAL_SAFE_BOX_9_16
from src.speaker_guard import compute_mfcc_lr_asd
from src.trends import _infer_hook_style
from src.voice_engine import COMMERCIAL_TTS_MODELS


def test_llm_router_dec1_migration_media_resolution_and_flex_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    cands = _vertex_candidates()
    models = [m for m, _ in cands]
    assert "gemini-3.8-flash" in models
    assert "gemini-3.5-flash-lite" in models
    assert all("2.5" not in m for m in models)

    settings_std = build_google_model_settings(media_resolution="MEDIA_RESOLUTION_MEDIUM", service_tier="standard")
    assert settings_std["google_video_resolution"] == MediaResolution.MEDIA_RESOLUTION_MEDIUM
    assert "google_cloud_service_tier" not in settings_std

    settings_flex = build_google_model_settings(media_resolution="MEDIA_RESOLUTION_HIGH", service_tier="flex")
    assert settings_flex["google_video_resolution"] == MediaResolution.MEDIA_RESOLUTION_HIGH
    assert settings_flex["google_cloud_service_tier"] == "flex_only"

    std_cost = _estimate_vertex_cost_usd(100_000, 0, 10_000, model_name="gemini-3.8-flash", service_tier="standard")
    flex_cost = _estimate_vertex_cost_usd(100_000, 0, 10_000, model_name="gemini-3.8-flash", service_tier="flex")
    assert flex_cost == pytest.approx(std_cost * 0.5, rel=1e-4)
    assert COMMERCIAL_TTS_MODELS["pocket-tts"]["license"] == "CC-BY-4.0"


def test_runtime_paths_cross_platform_helpers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert format_hms(60) == "00:01:00"
    assert format_hms(45) == "00:00:45"
    assert format_hms(3661) == "01:01:01"
    assert parse_iso_duration_sec("PT1M15S") == 75.0
    assert parse_iso_duration_sec("PT42S") == 42.0

    flags = headless_browser_flags()
    assert "--no-sandbox" in flags
    assert "--disable-dev-shm-usage" in flags

    fnt = load_bundled_font(28)
    assert fnt is not None
    assert resolve_binary("ffmpeg") is not None

    custom_omni = tmp_path / "omni_cache"
    monkeypatch.setenv("OMNI_RAG_CACHE_DIR", str(custom_omni))
    assert resolve_omni_rag_dir() == custom_omni

    sample_file = tmp_path / "a" / "b.bin"
    sample_file.parent.mkdir(parents=True)
    sample_file.write_bytes(b"x" * 128)
    assert dir_size_bytes(tmp_path) == 128


def test_psutil_resource_guard_and_unload_heavy_models() -> None:
    import src.design_engine as de
    import src.rag_engine as re_mod
    import src.speaker_guard as sg
    import src.transcriber as tr

    snap = snapshot()
    assert snap["total_ram_mb"] > 512
    assert 0.0 <= float(snap["used_ram_pct"]) <= 100.0
    allowed, reason, _ = heavy_local_allowed(min_free_ram_mb=64, min_free_disk_gb=0.1)
    assert allowed is True
    assert reason == "resources healthy"

    tr._MODEL_CACHE["dummy"] = object()
    sg._SESSIONS["dummy"] = object()  # type: ignore[assignment]
    de._REMBG_SESSIONS["dummy"] = object()
    re_mod._EMBEDDER_CACHE = object()
    re_mod._RERANKER_CACHE = object()

    res = unload_heavy_models()
    assert res["cleared"]["transcriber_models"] >= 1
    assert res["cleared"]["speaker_guard_sessions"] >= 1
    assert res["cleared"]["rembg_sessions"] >= 1
    assert res["cleared"]["rag_embedder"] == 1
    assert res["cleared"]["rag_reranker"] == 1
    assert len(tr._MODEL_CACHE) == 0
    assert len(sg._SESSIONS) == 0
    assert len(de._REMBG_SESSIONS) == 0
    assert re_mod._EMBEDDER_CACHE is None
    assert re_mod._RERANKER_CACHE is None


def test_rag_no_1600_truncation_7_skills_vec0_domain_and_reranker(tmp_path: Path) -> None:
    db_file = tmp_path / "rag_test.db"
    init_db(db_file)
    upsert_creator("custom_en_creator", "Custom EN Creator", tier="bounty_target", style_notes="Fast reaction zoom", db_path=db_file)

    summary = inspect_db_summary(db_file)
    assert "knowledge_chunks" not in summary["tables"]
    assert "knowledge_chunks" not in summary["counts"]

    chunks = collect_all_knowledge_chunks(PROJECT_ROOT, db_path=db_file)
    skill_paths = {c["source_path"] for c in chunks if c["source_path"].startswith(".agents/skills/")}
    assert len(skill_paths) == 7

    creator_fb = [c for c in chunks if c["chunk_id"] == "gta6_streamers:creator_db:custom_en_creator"]
    assert len(creator_fb) == 1

    # Verify all 10 golden references & all 10 viral_style_dna samples exist atomically (zero truncation loss)
    golden_chunks = [c for c in chunks if c["source_path"] == "context/golden_references.json"]
    dna_sample_chunks = [c for c in chunks if c["chunk_id"].startswith("shortform_craft:style_dna:sample_")]
    assert len(golden_chunks) >= 10
    assert len(dna_sample_chunks) >= 10

    # Deterministic 384d embedder to test native sqlite-vec `domain` pre-KNN filtering in isolation
    def _fake_embed(texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for idx, t in enumerate(texts):
            v = [0.0] * 384
            v[0] = 1.0 if "apca" in t.lower() else 0.9
            v[(idx % 380) + 1] = 0.1
            out.append(v)
        return out

    mini_chunks = [
        *[
            {
                "chunk_id": f"shortform_craft:bulk:{i:03d}",
                "source_path": "docs/style_bible.md",
                "domain": "shortform_craft",
                "heading": f"Bulk Shortform {i}",
                "content": f"Generic shortform rule item {i} with high vector similarity.",
            }
            for i in range(30)
        ],
        {
            "chunk_id": "design_ctr:tiny:001",
            "source_path": "docs/design_bible.md",
            "domain": "design_ctr",
            "heading": "APCA Perceptual Contrast Gate",
            "content": "Enforce APCA Lc >= 75 inside UNIVERSAL_SAFE_BOX_9_16.",
        },
    ]
    reb = rebuild_rag_index(db_path=db_file, chunks_override=mini_chunks, embed_fn=_fake_embed)
    assert reb["vec_enabled"] is True
    assert reb["vec_chunks"] == 31

    # Pure vector query on tiny `design_ctr` domain must succeed via vec0 pre-KNN `AND v.domain = ?` even when BM25 misses!
    hits_design = query_rag("visual legibility luminance", domain="design_ctr", top_k=3, db_path=db_file, embed_fn=_fake_embed)
    assert len(hits_design) == 1
    assert hits_design[0]["chunk_id"] == "design_ctr:tiny:001"
    assert hits_design[0]["vec_rank"] == 1

    conn = sqlite3.connect(str(db_file))
    try:
        ddl = str(conn.execute("SELECT sql FROM sqlite_master WHERE name='rag_chunks_vec'").fetchone()[0]).lower()
        assert "domain text" in ddl
    finally:
        conn.close()

    # Verify valuable psychology/QA docs are NOT flagged as webdev noise, while Jason Gregory's engine book IS blocked
    assert _is_webdev_noise_hit("books/hooked_habit_forming.md", "agent-skill", "Hook Model", "Trigger action variable reward") is False
    assert _is_webdev_noise_hit("books/nudge_thaler_sunstein.md", "psychology", "Choice Architecture", "Default options") is False
    assert _is_webdev_noise_hit("docs/CANON_AUTOMATED_VISUAL_QA_VLM_CRITIC_AND_CTO_HITL_GATE_2026.md", "qa", "VLM Gate", "Safezone") is False
    assert _is_webdev_noise_hit("architecture_and_books/game_engine_architecture.md", "books", "Jason Gregory", "C++ quaternion") is True
    assert (
        _passes_domain_guard(
            {"source": "omni_rag_library", "heading": "Game Engine Architecture", "content": "Jason Gregory C++ engine loop"},
            "GTA VI Jason Duval Lucia Leonida",
            None,
            ce_logit=2.5,
        )
        is False
    )


def test_ssrf_sound_regex_html_escape_irc_and_pretool_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. SSRF checks
    assert validate_public_http_url("http://127.0.0.1:8080/admin", resolve_dns=False)[0] is False
    assert validate_public_http_url("http://169.254.169.254/latest/meta-data/", resolve_dns=False)[0] is False
    assert validate_public_http_url("https://metadata.google.internal/computeMetadata/v1/", resolve_dns=False)[0] is False
    assert validate_public_http_url("http://192.168.1.10/campaign", resolve_dns=False)[0] is False
    assert is_valid_campaign_record("http://10.0.0.5/whop/camp", "GTA Campaign", 2.5)[0] is False

    # 2. Strict sound_campaign regex (must NOT trigger on "sound quality" or "game sound")
    from src.bounties import _SOUND_CAMPAIGN_RE

    assert _SOUND_CAMPAIGN_RE.search("Keep clean game sound quality and mic audio") is None
    assert _SOUND_CAMPAIGN_RE.search("Must use the official sound in TikTok app") is not None
    audit = audit_campaign_rules("GTA 6 Global Clipping", "Pay $2.00 per 1k views", cpm_usd=2.0, burn_rate_pct=40.0)
    assert audit["eligible_from_ar"] is True

    # 3. Telegram HTML escaping
    captured: list[str] = []

    def _fake_post(url: str, json: dict[str, Any], timeout: int = 15) -> Any:
        captured.append(str(json.get("text") or ""))

        class _Resp:
            status_code = 200

            @staticmethod
            def json() -> dict[str, Any]:
                return {"ok": True}

        return _Resp()

    monkeypatch.setattr("src.notifier._load_telegram_creds", lambda: ("dummy_tok", "12345"))
    monkeypatch.setattr("src.notifier._throttle", lambda: None)
    monkeypatch.setattr("src.notifier.curl_requests.post", _fake_post)

    send_clip_ready_alert(
        "run<1>",
        "clip&2",
        tmp_path / "<evil>.mp4",
        packaging={"shorts_title": "<script>alert(1)</script>", "suggested_on_screen_hook": "A & B"},
    )
    send_bounty_alert({"campaign_id": "id<1>", "platform": "whop", "title": "<b>Inject</b>", "cpm_usd": 2.0})
    assert "<script>" not in captured[0]
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in captured[0]
    assert "&lt;b&gt;Inject&lt;/b&gt;" in captured[1]

    # 4. Live Sniper IRC validation + HLS 403 refresh
    assert validate_channel_login("Jynxzi") == "jynxzi"
    with pytest.raises(ValueError):
        validate_channel_login("bad_chan\r\nPRIVMSG #victim :hi")

    refresh_calls: list[tuple[str, str]] = []
    stop_ev = asyncio.Event()

    def _fake_get(url: str, **kwargs: Any) -> Any:
        class _R:
            status_code = 403
            text = ""
            content = b""

        return _R()

    def _fake_resolve(plat: str, ch: str) -> tuple[str | None, int | None]:
        refresh_calls.append((plat, ch))
        stop_ev.set()
        return "https://example.com/refreshed.m3u8", None

    monkeypatch.setattr("src.live_sniper.curl_requests.get", _fake_get)
    monkeypatch.setattr("src.live_sniper.resolve_live_hls_url", _fake_resolve)
    monkeypatch.setattr("src.live_sniper.asyncio.wait_for", lambda fut, timeout: (fut.close(), asyncio.sleep(0))[1])
    asyncio.run(
        _poll_hls_ring_buffer(
            "https://example.com/expired.m3u8",
            HLSRingBuffer(max_seconds=10.0),
            stop_ev,
            platform="kick",
            channel="xqc",
        )
    )
    assert refresh_calls == [("kick", "xqc")]

    # 5. Security PreToolUse hook symmetric read_file + python -c blocking
    ws = PROJECT_ROOT
    deny_ext_read = evaluate_security_payload(
        {"toolCall": {"name": "read_file", "args": {"Path": str(ws.parent / "outside_secret.txt")}}},
        workspace_root=ws,
    )
    assert deny_ext_read["decision"] == "deny"

    allow_in_read = evaluate_security_payload(
        {"toolCall": {"name": "read_file", "args": {"Path": str(ws / "pyproject.toml")}}},
        workspace_root=ws,
    )
    assert allow_in_read["decision"] == "allow"

    deny_py_env = evaluate_security_payload(
        {"toolCall": {"name": "run_command", "args": {"CommandLine": 'python -c "import os; print(os.environ)"'}}},
        workspace_root=ws,
    )
    assert deny_py_env["decision"] == "deny"

    deny_py_mut = evaluate_security_payload(
        {"toolCall": {"name": "run_command", "args": {"CommandLine": 'python -c "from pathlib import Path; Path(\'x\').write_text(\'1\')"'}}},
        workspace_root=ws,
    )
    assert deny_py_mut["decision"] == "deny"


def test_media_check_visual_qa_design_engine_and_p1_fixes(tmp_path: Path) -> None:
    # 1. DesignEngine floating island cleaner & cover != overlay guard
    rgba = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    d = ImageDraw.Draw(rgba)
    d.rectangle([60, 40, 150, 190], fill=(255, 200, 180, 255))  # main portrait
    d.rectangle([10, 10, 18, 18], fill=(255, 255, 255, 255))  # tiny floating halo artifact
    cleaned = clean_alpha_floating_islands(rgba)
    arr_a = np.asarray(cleaned.getchannel("A"))
    assert int(arr_a[14, 14]) == 0
    assert int(arr_a[100, 100]) == 255

    hl, _ = ensure_complementary_cover_headline(
        "HE DIDN'T BACK UP",
        title="Bro Forgot Fire Spreads in GTA 6",
        fallback_headlines=["HE DIDNT BACK UP", "INSTANT VALHALLA"],
        forbidden_texts=["HE DIDN'T BACK UP"],
    )
    assert hl == "INSTANT VALHALLA"
    assert is_cover_overlay_duplicated("HE DIDN'T BACK UP", "HE DIDNT BACK UP") is True
    assert is_cover_overlay_duplicated("INSTANT VALHALLA", "HE DIDN'T BACK UP") is False

    # 2. Kept-segment output timestamp -> source offset remapping
    segs = [(0.0, 5.0), (8.0, 15.0)]
    assert map_output_ts_to_source_offset(3.0, segs) == 3.0
    assert map_output_ts_to_source_offset(6.5, segs) == 9.5

    # 3. Speaker guard MFCC & Trends hook classifier
    pcm = (np.sin(np.linspace(0, 50.0, 16000)) * 8000).astype(np.int16)
    mfcc = compute_mfcc_lr_asd(pcm, sample_rate=16000)
    assert mfcc.shape[1] == 13
    assert _infer_hook_style("Rockstar hid this Area 51 secret in GTA 6")[0] == "Secret / Lore Discovery"

    # 4. Synthetic 1080x1920@60 CFR MP4 -> check_ffprobe_spec + draw_safezones_overlay + evaluate_visual_qa
    mp4 = tmp_path / "clip_test_master_9_16.mp4"
    cover_jpg = tmp_path / "clip_test_cover_9_16.jpg"
    Image.new("RGB", (1080, 1920), (24, 28, 36)).save(cover_jpg, format="JPEG")
    subprocess.run(
        [
            str(resolve_binary("ffmpeg") or "ffmpeg"),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1080x1920:rate=60:duration=2.2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=2.2",
            "-af",
            "loudnorm=I=-14:TP=-1.5:LRA=11",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-fps_mode",
            "cfr",
            "-r",
            "60",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            str(mp4),
        ],
        check=True,
        timeout=60,
    )
    ff_spec = check_ffprobe_spec(mp4, expected_fps=60.0)
    assert ff_spec["passed"] is True
    assert ff_spec["is_cfr"] is True
    assert ff_spec["avg_fps"] == 60.0
    assert UNIVERSAL_SAFE_BOX_9_16 == (48, 420, 888, 1248)

    raw_png = tmp_path / "raw_frame.png"
    sz_png = tmp_path / "sz_frame.png"
    Image.new("RGB", (1080, 1920), (40, 50, 60)).save(raw_png)
    draw_safezones_overlay(raw_png, sz_png)
    assert sz_png.is_file()

    mc_res = evaluate_video(mp4, qa_dir=tmp_path / "qa", expected_fps=60.0)
    assert mc_res["passed"] is True

    def _mock_vlm_pass(_inp: VisualQAInputs, _imgs: list[Path], _rag: str) -> tuple[VisualQAVlmVerdict, str]:
        return (
            VisualQAVlmVerdict(
                no_hud_or_right_button_collision=True,
                no_cover_overlay_duplication=True,
                no_halos_or_floating_artifacts=True,
                face_unobstructed_on_cover=True,
                mobile_legibility_score=0.91,
                anti_ai_slop_score=0.90,
                overall_visual_score=0.90,
                recommended_parametric_fix="none",
                issues=[],
                rationale="Clean safe-zones, 60fps CFR, complementary cover.",
            ),
            "mock_vlm",
        )

    qa_pass = evaluate_visual_qa(
        VisualQAInputs(
            clip_id="clip_test",
            video_path=mp4,
            cover_path=cover_jpg,
            qa_dir=tmp_path / "qa",
            expected_fps=60.0,
            shorts_title="Bro Forgot Fire Spreads",
            cover_headline="INSTANT VALHALLA",
            overlay_callout_text="HE DIDN'T BACK UP",
        ),
        vlm_judge_fn=_mock_vlm_pass,
    )
    assert qa_pass["passed"] is True
    assert qa_pass["status"] == "READY_HITL"

    # Duplicated cover == overlay must fail deterministically and recommend `alternate_cover_hook`
    qa_dup_fail = evaluate_visual_qa(
        VisualQAInputs(
            clip_id="clip_test",
            video_path=mp4,
            cover_path=cover_jpg,
            qa_dir=tmp_path / "qa",
            expected_fps=60.0,
            shorts_title="Bro Forgot Fire Spreads",
            cover_headline="HE DIDN'T BACK UP",
            overlay_callout_text="HE DIDN'T BACK UP",
        ),
        vlm_judge_fn=_mock_vlm_pass,
    )
    assert qa_dup_fail["passed"] is False
    assert qa_dup_fail["status"] == "NEEDS_VISUAL_FIX"
    assert qa_dup_fail["recommended_fix"] == "alternate_cover_hook"

    # 5. GC protects runs/branding/ and purges old closed-run slice_01.mp4
    runs_tmp = tmp_path / "runs_gc"
    db_gc = runs_tmp / "studio.db"
    init_db(db_gc)
    brand_logo = runs_tmp / "branding" / "logo_safezones.png"
    brand_logo.parent.mkdir(parents=True)
    brand_logo.write_bytes(b"brand")

    closed_dir = runs_tmp / "run_done"
    closed_dir.mkdir(parents=True)
    (closed_dir / "state.json").write_text(json.dumps({"stage": "reviewed"}), encoding="utf-8")
    old_slice = closed_dir / "slice_01.mp4"
    old_slice.write_bytes(b"slice_bytes")

    gc_rep = run_storage_gc(runs_dir=runs_tmp, db_path=db_gc, ttl_hours=0.0, dry_run=False)
    assert brand_logo.is_file()
    assert not old_slice.exists()
    assert gc_rep["deleted_count"] >= 1

    # 6. Compiler resolves native 16:9 slice + start_trim/end_trim (and filters dmca_flag=1 under strict_clean)
    db_comp = tmp_path / "comp.db"
    init_db(db_comp)
    native_16_9 = tmp_path / "native_slice.mp4"
    subprocess.run(
        [
            str(resolve_binary("ffmpeg") or "ffmpeg"),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1920x1080:rate=30:duration=4.0",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=330:sample_rate=48000:duration=4.0",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(native_16_9),
        ],
        check=True,
        timeout=60,
    )
    upsert_slice("r1", "slice_01", "https://twitch.tv/videos/1", "twitch", 0.0, 4.0, "twitch_chat_zscore", 5.0, str(native_16_9), dmca_flag=False, db_path=db_comp)
    upsert_clip("r1", "clip_01", "slice_01", "https://twitch.tv/videos/1", "jynxzi", 0.5, 3.0, "split_webcam_top", 1.2, True, "Jason Duval", 0.85, True, -14.0, -1.2, str(mp4), status="READY_HITL", db_path=db_comp)

    captured_specs: list[dict[str, Any]] = []

    def _spy_compile(specs: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        captured_specs.extend(specs)
        return {"ok": True, "count": len(specs)}

    import src.compiler as comp_mod

    orig_compile = comp_mod.compile_longform_16_9
    comp_mod.compile_longform_16_9 = _spy_compile  # type: ignore[assignment]
    try:
        res_c = compile_from_studio_db(tmp_path / "out_comp", db_path=db_comp)
        assert res_c["count"] == 1
        assert captured_specs[0]["path"] == str(native_16_9)
        assert captured_specs[0]["start_trim"] == 0.5
        assert captured_specs[0]["end_trim"] == 3.0
    finally:
        comp_mod.compile_longform_16_9 = orig_compile  # type: ignore[assignment]
