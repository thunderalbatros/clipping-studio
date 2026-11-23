"""Unit tests for Fase B: 5-network ClipPackaging, Style DNA (/steal), and Bounty Radar HITL."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.bounties import approve_bounty, audit_campaign_rules, get_bounties
from src.db import init_db, upsert_campaign
from src.schemas import ClipPackaging
from src.style_analyzer import synthesize_style_dna


def test_clip_packaging_supports_all_5_networks_and_validates_x_length() -> None:
    pkg = ClipPackaging(
        clip_id="clip_01",
        shorts_title="Jynxzi Instantly Regrets Detonating Jason's Car",
        shorts_hashtags=["#GTA6", "#Shorts", "#Jynxzi"],
        tiktok_caption="Would you have survived this in GTA VI?",
        suggested_on_screen_hook="HE BLEW HIMSELF UP",
        reels_caption="Send this to your duo who always ruins the heist.",
        facebook_reels_caption="GTA VI physics already humbling streamers on day one. Who else did this?",
        x_post_text="Jynxzi really tried to blow up Donnie's car in #GTA6 and immediately sent himself to Valhalla.",
        matched_lore_entity="Jason Duval",
        bounty_safe=True,
    )
    assert pkg.bounty_safe is True
    assert "#GTA6" in pkg.x_post_text

    with pytest.raises(ValidationError):
        ClipPackaging(
            clip_id="clip_02",
            shorts_title="Valid Title",
            shorts_hashtags=["#GTA6", "#Shorts", "#Gaming"],
            tiktok_caption="ok",
            suggested_on_screen_hook="NO WAY",
            reels_caption="ok",
            x_post_text="x" * 280,
        )


def test_bounty_rules_detector_flags_geo_lock_traps_and_burn_rate() -> None:
    bad = audit_campaign_rules(
        title="GTA 6 US Clippers Only",
        brief_text="US residents only, W-9 required. Exclusive rights. 92% paid out.",
        cpm_usd=1.5,
        burn_rate_pct=92.0,
    )
    assert bad["eligible_from_ar"] is False
    assert bad["verdict_recommendation"] == "REJECT"
    assert any("GEO_BLOCK" in f for f in bad["risk_flags"])
    assert any("HIGH_BURN_RATE" in f for f in bad["risk_flags"])

    good = audit_campaign_rules(
        title="GTA VI Global Highlights",
        brief_text="Worldwide payouts via crypto/Wise. No AI voice, original audio only.",
        cpm_usd=2.0,
        burn_rate_pct=45.0,
        min_views=5000,
    )
    assert good["eligible_from_ar"] is True
    assert good["voice_policy"] == "none"
    assert good["verdict_recommendation"] == "APPROVE_CANDIDATE"


def test_bounty_sqlite_two_reading_burn_delta_and_hitl_persistence(tmp_path: Path) -> None:
    db_file = tmp_path / "bounties.db"
    init_db(db_file)
    r1 = upsert_campaign("whop_test1", "whop", "GTA VI Clips", cpm_usd=2.0, burn_rate_pct=25.0, db_path=db_file)
    assert r1["verdict"] == "PENDING_HITL"
    assert r1["burn_delta_pct"] is None

    assert approve_bounty("whop_test1", "APPROVED", db_path=db_file) is True
    # Second scan reading should compute burn_delta_pct and PRESERVE human APPROVED verdict
    r2 = upsert_campaign("whop_test1", "whop", "GTA VI Clips", cpm_usd=2.0, burn_rate_pct=38.5, db_path=db_file)
    assert r2["burn_delta_pct"] == 13.5
    assert r2["verdict"] == "APPROVED"
    assert len(get_bounties(verdict="APPROVED", db_path=db_file)) == 1


def test_synthesize_style_dna_persists_medians(tmp_path: Path) -> None:
    out = tmp_path / "viral_style_dna.json"
    samples = [
        {"label": "a.mp4", "duration_sec": 22.0, "cuts_per_min": 18.0, "avg_shot_len_sec": 3.1, "opening_3s_lufs": -14.0, "first_cut_sec": 2.4},
        {"label": "b.mp4", "duration_sec": 28.0, "cuts_per_min": 14.0, "avg_shot_len_sec": 4.2, "opening_3s_lufs": -15.0, "first_cut_sec": 3.0},
    ]
    dna = synthesize_style_dna(samples, out_path=out, persist=True)
    assert out.exists()
    assert dna["sample_count"] == 2
    assert dna["empirical_medians"]["duration_sec"] == 25.0
    assert dna["recommended_targets"]["first_visual_pattern_interrupt_sec"] <= 4.0
