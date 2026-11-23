"""Unit tests for Fase C: Hawkes/ChatWave detector, anti-subgift filter, HLSRingBuffer, and Telegram notifier."""

from __future__ import annotations

from pathlib import Path

from src.live_sniper import ChatWaveDetector, HawkesIntensityEngine, HLSRingBuffer
from src.notifier import send_bounty_alert, send_clip_ready_alert, send_telegram_message


def test_hawkes_intensity_rises_on_burst_and_decays() -> None:
    hk = HawkesIntensityEngine()
    i1 = hk.update(10.0, weight=1.5)
    i2 = hk.update(10.5, weight=1.5)
    assert i2 > i1
    later = hk.current(40.0)
    assert later < i2
    assert later >= hk.mu


def test_chat_wave_detector_fires_on_reaction_spike_and_ignores_subgifts() -> None:
    det = ChatWaveDetector("twitch:jynxzi", "twitch", "jynxzi", min_burst_msgs=6, min_intensity=4.5, cooldown_sec=60.0)

    # Establish quiet baseline across 4 buckets (20s)
    t = 100.0
    for _ in range(4):
        det.ingest("hello chat", now=t)
        t += 5.1

    # Subgift spam must NOT trigger a clip
    for i in range(12):
        trig = det.ingest("User123 gifted a Tier 1 sub to Viewer!", now=t + i * 0.2)
        assert trig is None
    assert det.ignored_subgifts == 12

    # Real gameplay chaos burst MUST trigger once, then respect cooldown
    fired = None
    for i, msg in enumerate(["NO WAY", "CLIP IT", "LULW", "WTF BRO", "OMEGALUL", "INSANE", "LMAO", "CLIP IT"]):
        trig = det.ingest(msg, now=t + 3.0 + i * 0.25)
        if trig is not None:
            fired = trig
    assert fired is not None
    assert fired.reaction_ratio >= 0.5
    assert det.ingest("CLIP IT AGAIN", now=t + 6.0) is None  # cooldown active


def test_hls_ring_buffer_enforces_duration_and_ram_caps() -> None:
    ring = HLSRingBuffer(max_seconds=10.0, max_ram_mb=1.0)
    chunk = b"\x47" * (300 * 1024)  # 300 KB synthetic MPEG-TS packet bytes
    for i in range(8):
        ring.append(f"https://cdn.example/seg_{i}.ts", 2.0, chunk)
    assert ring.ram_mb <= 1.0
    assert ring.duration_sec <= 10.0
    payload, dur = ring.snapshot_bytes()
    assert len(payload) > 0
    assert dur >= 4.0


def test_telegram_notifier_dry_run_formats_cleanly(tmp_path: Path) -> None:
    res1 = send_telegram_message("<b>Ping</b>", dry_run=True)
    assert res1["ok"] is True and res1["dry_run"] is True
    res2 = send_clip_ready_alert(
        "run_01",
        "clip_01",
        tmp_path / "clip.mp4",
        packaging={"shorts_title": "GTA VI Chaos", "suggested_on_screen_hook": "NO WAY", "x_post_text": "Wild clip #GTA6"},
        critic_overall=0.91,
        dry_run=True,
    )
    assert res2["ok"] is True
    res3 = send_bounty_alert({"campaign_id": "whop_1", "platform": "whop", "title": "GTA VI", "cpm_usd": 2.0}, dry_run=True)
    assert res3["ok"] is True
