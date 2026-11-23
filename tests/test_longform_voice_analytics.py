"""Unit tests for Fase D: Long-Form 16:9 Compiler, KS-4 Commercial Voice Engine, and Post-Publish Analytics."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.analytics import record_publish_metrics, summarize_performance
from src.compiler import _fmt_chapter_ts
from src.db import init_db
from src.voice_engine import (
    assert_voice_policy_allowed,
    master_voice_wav,
    prepare_human_batch_sheet,
)


def test_voice_engine_enforces_rule_03_and_ks4_license_killswitch() -> None:
    with pytest.raises(ValueError, match="voice_mode='none'"):
        assert_voice_policy_allowed("none")
    with pytest.raises(ValueError, match="bounty"):
        assert_voice_policy_allowed("tts", is_bounty_clip=True, model_name="kokoro-82m")
    with pytest.raises(ValueError, match="KS-4"):
        assert_voice_policy_allowed("tts", is_bounty_clip=False, model_name="XTTS-v2")
    with pytest.raises(ValueError, match="KS-4"):
        assert_voice_policy_allowed("tts", is_bounty_clip=False, model_name="k2-fsa/OmniVoice")

    # Commercial-safe Apache-2.0 / MIT models pass for owned-channel non-bounty clips
    assert_voice_policy_allowed("tts", is_bounty_clip=False, model_name="kokoro-82m")
    assert_voice_policy_allowed("tts", is_bounty_clip=False, model_name="pocket-tts")
    assert_voice_policy_allowed("human_batch", is_bounty_clip=True)


def test_human_batch_sheet_and_audio_mastering(tmp_path: Path) -> None:
    res = prepare_human_batch_sheet(
        [{"clip_id": "clip_01", "cold_open_script": "You won't believe what Jynxzi just detonated in Vice City."}],
        tmp_path / "sm58_batch",
    )
    assert res["count"] == 1
    assert Path(res["sheet_md"]).exists()
    assert Path(res["manifest_json"]).exists()

    # Master a real CC0 SFX WAV through the FFmpeg broadcast chain
    sample_wav = Path("assets/sfx/whoosh.wav")
    if sample_wav.exists():
        out_wav = master_voice_wav(sample_wav, tmp_path / "mastered.wav")
        assert out_wav.exists() and out_wav.stat().st_size > 1000


def test_analytics_record_and_summarize_multi_network(tmp_path: Path) -> None:
    db_file = tmp_path / "analytics.db"
    init_db(db_file)
    record_publish_metrics(
        "run_01",
        "clip_02",
        "youtube_shorts",
        views=42_500,
        likes=3_100,
        shares=620,
        avg_view_pct=108.5,
        streamer_id="jynxzi",
        hook_style="Physics Chaos",
        db_path=db_file,
    )
    record_publish_metrics(
        "run_01",
        "clip_02",
        "tiktok",
        views=88_000,
        likes=7_400,
        shares=1_900,
        avg_view_pct=94.0,
        streamer_id="jynxzi",
        hook_style="Physics Chaos",
        db_path=db_file,
    )
    fb_json = tmp_path / "feedback.json"
    summary = summarize_performance(db_path=db_file, out_path=fb_json)
    assert fb_json.exists()
    assert summary["tracked_posts"] == 2
    assert summary["total_views"] == 130_500
    assert summary["top_streamers"][0]["streamer_id"] == "jynxzi"
    assert _fmt_chapter_ts(125.0) == "02:05"
    assert _fmt_chapter_ts(3661.0) == "01:01:01"
