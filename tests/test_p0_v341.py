"""Unit tests for P0 Final v3.4.1: Security Guard, Storage GC, 3-Mode Audio Policy, Bounties, Whitelist Bypass, Anti-Noise Federated RAG & Visual Contracts."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from scripts.security_pretool_hook import evaluate_security_payload
from src.analytics import emit_hitl_kit_5_networks, record_publish_metrics
from src.bounties import (
    audit_campaign_rules,
    get_bounties,
    import_bounty_hitl,
    is_valid_campaign_record,
    purge_junk_campaigns,
)
from src.captions import generate_karaoke_ass
from src.curator import _apply_pass1_hard_gates, _checked_by_code, _compute_sound_campaign_climax_ms
from src.db import init_db, upsert_campaign, upsert_clip
from src.design_engine import ensure_complementary_cover_headline
from src.gc import run_storage_gc
from src.miner import validate_streamer_whitelist
from src.rag_engine import query_omni_rag_federated, rebuild_rag_index
from src.renderer import compute_adaptive_layout, compute_webcam_card_pos
from src.schemas import (
    AnnotatedTranscript,
    ClipPackaging,
    EditPlan,
    FreezeFrameSpec,
    NonVerbalEvent,
    SliceAuditResult,
    WordTimestamp,
)
from src.transcriber import evaluate_audio_policy_yamnet


def test_security_pretool_guard_blocks_external_writes_and_secret_reads(tmp_path: Path) -> None:
    ws = tmp_path / "clipping studio"
    ws.mkdir()

    # Allowed: normal write inside workspace (with space in path) and reading .env.example
    assert (
        evaluate_security_payload(
            {"toolCall": {"name": "write_file", "args": {"Path": str(ws / "src" / "foo.py")}}},
            workspace_root=ws,
        )["decision"]
        == "allow"
    )
    assert (
        evaluate_security_payload(
            {"toolCall": {"name": "read_file", "args": {"Path": str(ws / ".env.example")}}},
            workspace_root=ws,
        )["decision"]
        == "allow"
    )

    # Denied: reading .env or cookies.txt
    assert (
        evaluate_security_payload(
            {"toolCall": {"name": "read_file", "args": {"Path": str(ws / ".env")}}},
            workspace_root=ws,
        )["decision"]
        == "deny"
    )
    assert (
        evaluate_security_payload(
            {"toolCall": {"name": "read_file", "args": {"Path": str(ws / "yt_cookies.txt")}}},
            workspace_root=ws,
        )["decision"]
        == "deny"
    )

    # Denied: shell dumping .env or env vars
    assert (
        evaluate_security_payload(
            {"toolCall": {"name": "run_command", "args": {"CommandLine": "Get-Content .env"}}},
            workspace_root=ws,
        )["decision"]
        == "deny"
    )
    assert (
        evaluate_security_payload(
            {"toolCall": {"name": "run_command", "args": {"CommandLine": "cat .env"}}},
            workspace_root=ws,
        )["decision"]
        == "deny"
    )

    # Denied: writes outside workspace (including ~/.gemini/config and sibling repos)
    for bad_path in (
        r"C:\Users\tomas\.gemini\config\mcp_config.json",
        r"C:\Users\tomas\Desktop\web-software-factory\README.md",
        r"C:\Users\tomas\Desktop\centinela-studio\x.py",
        r"C:\Users\tomas\Desktop\streamerbot\x.py",
        str(tmp_path / "outside.txt"),
    ):
        res = evaluate_security_payload(
            {"toolCall": {"name": "write_file", "args": {"Path": bad_path}}},
            workspace_root=ws,
        )
        assert res["decision"] == "deny", f"Expected deny for {bad_path}"

    # Denied: shell write to external repo
    assert (
        evaluate_security_payload(
            {
                "toolCall": {
                    "name": "run_command",
                    "args": {"CommandLine": r'Set-Content "C:\Users\tomas\Desktop\web-software-factory\hack.txt" "x"'},
                }
            },
            workspace_root=ws,
        )["decision"]
        == "deny"
    )


def test_storage_gc_preserves_db_and_ready_hitl_while_purging_disposables(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    run_a = runs_dir / "run_01"
    qa_dir = run_a / "clip_01_qa_frames"
    qa_dir.mkdir(parents=True)
    db_path = runs_dir / "studio.db"
    init_db(db_path)

    ready_mp4 = run_a / "clip_01_master_9_16.mp4"
    pub_mp4 = run_a / "clip_02_master_9_16.mp4"
    old_wav = run_a / "slice_01_16k.wav"
    old_qa = qa_dir / "frame_1.png"
    for p in (ready_mp4, pub_mp4, old_wav, old_qa):
        p.write_bytes(b"0" * 4096)

    # Backdate files older than 72h
    old_ts = 1_600_000_000.0
    for p in (old_wav, old_qa, qa_dir):
        os.utime(p, (old_ts, old_ts))

    upsert_clip(
        run_id="run_01",
        clip_id="clip_01",
        slice_id="slice_01",
        source_url="https://twitch.tv/videos/1",
        streamer_id="jynxzi",
        start_trim=0.0,
        end_trim=20.0,
        layout_mode="split_webcam_top",
        webcam_upscale_factor=1.2,
        no_webcam_gameplay_overlap=True,
        matched_lore_entity="Jason",
        critic_overall=0.85,
        media_check_passed=True,
        lufs=-14.0,
        dbtp=-1.2,
        output_mp4=str(ready_mp4),
        db_path=db_path,
    )
    upsert_clip(
        run_id="run_01",
        clip_id="clip_02",
        slice_id="slice_02",
        source_url="https://twitch.tv/videos/1",
        streamer_id="jynxzi",
        start_trim=0.0,
        end_trim=20.0,
        layout_mode="split_webcam_top",
        webcam_upscale_factor=1.2,
        no_webcam_gameplay_overlap=True,
        matched_lore_entity="Jason",
        critic_overall=0.85,
        media_check_passed=True,
        lufs=-14.0,
        dbtp=-1.2,
        output_mp4=str(pub_mp4),
        db_path=db_path,
    )
    # Mark clip_02 PUBLISHED in studio.db
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("UPDATE clips SET status = 'PUBLISHED' WHERE clip_id = 'clip_02'")

    dry = run_storage_gc(runs_dir=runs_dir, db_path=db_path, ttl_hours=72.0, dry_run=True)
    assert dry["dry_run"] is True
    assert dry["deleted_count"] == 0
    assert dry["candidates_count"] >= 3
    assert ready_mp4.exists() and pub_mp4.exists() and old_wav.exists()

    live = run_storage_gc(runs_dir=runs_dir, db_path=db_path, ttl_hours=72.0, dry_run=False)
    assert live["deleted_count"] >= 3
    assert db_path.exists()
    assert ready_mp4.exists()  # READY_HITL strictly preserved!
    assert not pub_mp4.exists()  # PUBLISHED purged
    assert not old_wav.exists()  # intermediate WAV purged
    assert not qa_dir.exists()  # old QA dir purged


def test_audio_policy_three_modes_yamnet_and_curator_gates() -> None:
    # 1. strict_clean flags music >= 0.45 and marks needs_demucs_rescue
    flag_s, demucs_s, adv_s = evaluate_audio_policy_yamnet(0.74, audio_policy="strict_clean")
    assert flag_s is True and demucs_s is True and "Demucs" in adv_s

    # 2. sound_campaign flags foreign background music and approves clean audio
    flag_sc_bad, _, _ = evaluate_audio_policy_yamnet(0.62, audio_policy="sound_campaign")
    flag_sc_ok, _, adv_sc_ok = evaluate_audio_policy_yamnet(0.12, audio_policy="sound_campaign")
    assert flag_sc_bad is True
    assert flag_sc_ok is False and "Official Sound" in adv_sc_ok

    # 3. keep_original NEVER flags/rejects, emits advisory instead
    flag_k, demucs_k, adv_k = evaluate_audio_policy_yamnet(0.88, audio_policy="keep_original")
    assert flag_k is False and demucs_k is False and "keep_original_advisory" in adv_k

    tr_music = AnnotatedTranscript(
        slice_id="s1",
        language="en",
        full_text="Watch this crazy explosion right now bro no way",
        words=[WordTimestamp(word=w, start=i * 0.8 + 1.0, end=i * 0.8 + 1.4) for i, w in enumerate(["Watch", "this", "crazy", "explosion", "right", "now", "bro"])],
        non_verbal_events=[
            NonVerbalEvent(kind="music_spike", start=2.0, end=8.0, intensity=0.82),
            NonVerbalEvent(kind="shouting", start=12.0, end=14.0, intensity=50.0),
        ],
        music_prob_peak=0.82,
        dmca_flag=True,
    )
    audit_keep = SliceAuditResult(
        slice_id="s1",
        scene_type="active_gameplay",
        visual_hook_score=0.9,
        dmca_clear=False,
        self_contained_score=90,
        suggested_start_sec=0.0,
        suggested_end_sec=25.0,
    )
    res_keep = _apply_pass1_hard_gates(audit_keep, tr_music, audio_policy="keep_original")
    assert res_keep.approved_for_pass2 is True
    assert res_keep.dmca_clear is True
    assert "keep_original_advisory" in res_keep.dmca_advisory

    audit_sc = SliceAuditResult(
        slice_id="s1",
        scene_type="active_gameplay",
        visual_hook_score=0.9,
        dmca_clear=True,
        self_contained_score=90,
        suggested_start_sec=0.0,
        suggested_end_sec=25.0,
    )
    res_sc = _apply_pass1_hard_gates(audit_sc, tr_music, audio_policy="sound_campaign")
    assert res_sc.approved_for_pass2 is False

    climax_ms = _compute_sound_campaign_climax_ms(
        0.0,
        25.0,
        tr_music,
        FreezeFrameSpec(timestamp_sec=11.5, box_2d=(200, 200, 500, 500), callout_text="BOOM", duration_sec=0.8),
    )
    assert climax_ms == 11500

    plan_keep = EditPlan(
        clip_id="clip_01",
        slice_id="s1",
        source_url="https://twitch.tv/videos/1",
        start_trim=0.0,
        end_trim=22.0,
        scene_type="active_gameplay",
        visual_hook_score=0.9,
        hook_headline="Crazy car explosion",
        dmca_clear=False,
        audio_policy="keep_original",
        self_contained_score=85,
    )
    ok, issues = _checked_by_code(plan_keep, tr_music, max_dur=30.0)
    assert ok is True, f"keep_original should pass _checked_by_code, got {issues}"


def test_bounties_hard_filter_geo_priority_and_hitl_import(tmp_path: Path) -> None:
    # Hard filter rejects blogs, YouTube, App Store, homepages, and cpm <= 0
    assert is_valid_campaign_record("https://www.youtube.com/watch?v=abc", "Video", 2.0)[0] is False
    assert is_valid_campaign_record("https://play.google.com/store/apps/details?id=x", "App", 2.0)[0] is False
    assert is_valid_campaign_record("https://clipping.net/", "Home", 2.0)[0] is False
    assert is_valid_campaign_record("https://whop.com/blog/how-to-clip", "Blog", 2.0)[0] is False
    assert is_valid_campaign_record("https://whop.com/real-campaign/", "GTA 6 Clips", 0.0)[0] is False
    assert is_valid_campaign_record("https://whop.com/gta6-rewards/", "GTA 6 Rewards", 2.5)[0] is True

    # Geo audit flags TikTok/IG tier1_60pct risk from Argentina
    tier1_audit = audit_campaign_rules(
        title="US Clipping Campaign",
        brief_text="Requires 60% US audience on TikTok",
        cpm_usd=2.0,
        burn_rate_pct=40.0,
        geo_requirement="tier1_60pct",
        platforms="tiktok,instagram_reels",
    )
    assert tier1_audit["geo_requirement"] == "tier1_60pct"
    assert any("GEO_LOCK_RISK" in f for f in tier1_audit["risk_flags"])

    db_file = tmp_path / "bounties.db"
    init_db(db_file)
    # Seed junk + real campaigns and purge junk
    upsert_campaign("junk_1", "vyro", "I Tried Every Clipping Platform", cpm_usd=0.0, brief_text="URL: https://youtube.com/watch?v=1", db_path=db_file)
    import_bounty_hitl(
        platform="whop",
        title="Tier1 TikTok Campaign",
        url="https://whop.com/tier1-tiktok-camp/",
        cpm_usd=3.0,
        burn_rate_pct=40.0,
        geo_requirement="tier1_60pct",
        platforms="tiktok",
        audio_policy="sound_campaign",
        db_path=db_file,
    )
    import_bounty_hitl(
        platform="whop",
        title="Global YT Shorts Gaming",
        url="https://whop.com/global-gaming-clips/",
        cpm_usd=2.0,
        burn_rate_pct=35.0,
        geo_requirement="global",
        platforms="youtube_shorts,x",
        audio_policy="strict_clean",
        db_path=db_file,
    )
    purged = purge_junk_campaigns(db_path=db_file)
    assert purged["removed_count"] == 1
    assert purged["kept_count"] == 2

    ordered = get_bounties(db_path=db_file)
    # Global campaign prioritized FIRST over higher-CPM tier1_60pct campaign!
    assert ordered[0]["geo_requirement"] == "global"
    assert ordered[1]["geo_requirement"] == "tier1_60pct"

    with pytest.raises(ValueError, match="Rejected invalid bounty"):
        import_bounty_hitl(
            platform="whop",
            title="Zero CPM",
            url="https://whop.com/zero/",
            cpm_usd=0.0,
            db_path=db_file,
        )


def test_whitelist_bypass_returns_dynamic_profile_for_arbitrary_creator() -> None:
    prof = validate_streamer_whitelist("random_bounty_creator_999")
    assert prof["dynamic_profile"] is True
    assert prof["display_name"] == "random_bounty_creator_999"
    assert "twitch" in prof["platforms"]


def test_federated_rag_filters_webdev_noise_and_ingests_publish_metrics(tmp_path: Path) -> None:
    fake_omni = tmp_path / "omni_cache"
    fake_omni.mkdir()
    hot_db = fake_omni / "rag_hot.sqlite"
    con = sqlite3.connect(str(hot_db))
    con.executescript(
        """
        CREATE TABLE docs (doc_id INTEGER PRIMARY KEY, rel_path TEXT, title TEXT, partition TEXT, category TEXT);
        CREATE TABLE chunks (chunk_id INTEGER PRIMARY KEY, doc_id INTEGER, chunk_uid TEXT, heading_path TEXT, text TEXT);
        CREATE VIRTUAL TABLE chunks_fts USING fts5(heading_path, text, content='chunks', content_rowid='chunk_id');
        INSERT INTO docs VALUES (1, 'frontend/react_hook_form.md', 'React Hook Form Guide', 'hot', 'frontend');
        INSERT INTO docs VALUES (2, 'books/hooked_habit_forming.md', 'Hooked - Nir Eyal', 'hot', 'psychology');
        INSERT INTO chunks VALUES (1, 1, 'rhf-1', 'useForm Hook', 'React Hook Form useState validation for Next.js frontend forms.');
        INSERT INTO chunks VALUES (2, 2, 'hooked-1', 'The Hook Model', 'Trigger, Action, Variable Reward hook loop captures user attention.');
        INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild');
        """
    )
    con.commit()
    con.close()

    hits = query_omni_rag_federated("hook", limit=4, omni_dir=fake_omni)
    assert len(hits) == 1
    assert "Hooked" in hits[0]["source_path"] or "Variable Reward" in hits[0]["content"]
    assert not (fake_omni / "rag_hot.sqlite-wal").exists()
    assert not (fake_omni / "rag_hot.sqlite-shm").exists()

    # Verify analytics publish_metrics flow into episodic_memory
    db_file = tmp_path / "studio_ep.db"
    init_db(db_file)
    record_publish_metrics(
        "run_smoke",
        "clip_02",
        "youtube_shorts",
        views=25000,
        shares=500,
        avg_view_pct=102.0,
        vvsa_pct=78.5,
        cpm_usd=2.2,
        streamer_id="jynxzi",
        hook_style="Physics Chaos",
        db_path=db_file,
    )
    reb = rebuild_rag_index(db_path=db_file, project_root=tmp_path, skip_vectors=True)
    assert reb["domains"].get("episodic_memory", 0) >= 1


def test_complementary_cover_headline_centered_layout_stutter_and_hitl_kit(tmp_path: Path) -> None:
    # Complementary cover headline never repeats content words from title
    chosen, overlap = ensure_complementary_cover_headline(
        "FIRE SPREADS FAST",
        title="Bro Forgot Fire Spreads in GTA 6",
        fallback_headlines=["HE DIDN'T BACK UP", "WAIT FOR IT"],
    )
    assert overlap == []
    assert chosen == "HE DIDN'T BACK UP"
    assert "FIRE" not in chosen and "SPREADS" not in chosen

    # Jynxzi corner webcam -> centered gameplay around x=960, zero overlap, safe card y>=180
    layout = compute_adaptive_layout(1920, 1080, (1354, 640, 566, 440), face_box_norm=(0.35, 0.05, 0.75, 0.65))
    assert layout["no_webcam_gameplay_overlap"] is True
    gx, _gy, gw, _gh = layout["gameplay_crop_px"]
    assert abs((gx + gw / 2.0) - 960.0) <= 4.0
    cw, ch = layout["webcam_card_size"]
    _, py = compute_webcam_card_pos(cw, ch)
    assert py >= 180

    # Single-letter stutter variants ("J", "J-", "J'") filtered in captions
    tr = AnnotatedTranscript(
        slice_id="s1",
        language="en",
        full_text="J J- J' Jason watch out",
        words=[
            WordTimestamp(word="J", start=1.0, end=1.1),
            WordTimestamp(word="J-", start=1.2, end=1.3),
            WordTimestamp(word="J'", start=1.4, end=1.5),
            WordTimestamp(word="Jason", start=1.6, end=2.0),
            WordTimestamp(word="watch", start=2.1, end=2.4),
        ],
    )
    plan = EditPlan(
        clip_id="clip_01",
        slice_id="s1",
        source_url="https://twitch.tv/videos/1",
        start_trim=0.0,
        end_trim=20.0,
        hook_headline="Watch out",
        self_contained_score=90,
    )
    ass_p = tmp_path / "subs.ass"
    generate_karaoke_ass(tr, plan, ass_p)
    txt = ass_p.read_text(encoding="utf-8")
    assert "JASON" in txt
    assert "}J-" not in txt and "}J'" not in txt

    # HITL Kit 5 networks emission
    pkg = ClipPackaging(
        clip_id="clip_02",
        shorts_title="Bro Forgot Fire Spreads in GTA 6",
        shorts_hashtags=["#GTA6", "#Shorts", "#Gaming"],
        tiktok_caption="Would you survive this?",
        suggested_on_screen_hook="HE DIDN'T BACK UP",
        reels_caption="Send to your duo",
        facebook_reels_caption="Classic GTA physics",
        x_post_text="Instant Valhalla #GTA6",
        cover_timestamp_ms=14200,
        cover_9_16_path=str(tmp_path / "cover.jpg"),
        ab_hook_variants=["HE DIDN'T BACK UP", "INSTANT KARMA"],
        author_reply_seed="Did he really stand on the gas can?",
    )
    kit = emit_hitl_kit_5_networks(pkg, tmp_path / "clip.mp4", tmp_path)
    assert Path(kit["kit_md_path"]).is_file()
    assert Path(kit["kit_json_path"]).is_file()
    assert set(kit["networks"].keys()) == {"youtube_shorts", "tiktok", "instagram_reels", "facebook_reels", "x"}
