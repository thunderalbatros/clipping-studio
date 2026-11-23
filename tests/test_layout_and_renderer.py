"""Unit tests for adaptive layout, safe-zone webcam anchor, profanity bleeper, jump-cuts, and punch-ins."""

from __future__ import annotations

import numpy as np

from src.renderer import (
    compute_adaptive_layout,
    compute_jump_cut_segments,
    compute_webcam_card_pos,
    find_opening_profanity_spans,
    find_punch_in_windows,
    rects_overlap,
)
from src.schemas import AnnotatedTranscript, NonVerbalEvent, WordTimestamp


def test_adaptive_layout_small_left_cam_anti_upscale_and_zero_overlap() -> None:
    layout = compute_adaptive_layout(1920, 1080, (18, 778, 326, 292), max_upscale=1.8, exclusion_pad=48)
    assert layout["layout_mode"] == "split_webcam_top"
    assert layout["is_framed_card"] is True
    assert layout["webcam_upscale_factor"] <= 1.8
    assert layout["no_webcam_gameplay_overlap"] is True
    gx, gy, gw, gh = layout["gameplay_crop_px"]
    assert not rects_overlap((gx, gy, gx + gw, gy + gh), layout["forbidden_webcam_rect"])


def test_adaptive_layout_right_cam_and_card_safe_y() -> None:
    layout = compute_adaptive_layout(1920, 1080, (1354, 640, 566, 440), face_box_norm=(0.35, 0.05, 0.75, 0.65))
    assert layout["no_webcam_gameplay_overlap"] is True
    assert layout["is_framed_card"] is True
    cw, ch = layout["webcam_card_size"]
    px, py = compute_webcam_card_pos(cw, ch)
    assert 0 <= px < 1080
    assert py >= 172
    assert py + ch <= 686


def test_adaptive_layout_full_gameplay_fallback() -> None:
    layout = compute_adaptive_layout(1920, 1080, None)
    assert layout["layout_mode"] == "full_gameplay_9_16"
    assert layout["no_webcam_gameplay_overlap"] is True


def test_opening_profanity_spans_only_inside_7_5s_shield() -> None:
    words = [
        WordTimestamp(word="Oh", start=10.2, end=10.4),
        WordTimestamp(word="fuck!", start=11.5, end=11.9),
        WordTimestamp(word="later", start=22.0, end=22.3),
        WordTimestamp(word="shit", start=23.0, end=23.4),
    ]
    spans = find_opening_profanity_spans(words, start_trim=10.0, shield_sec=7.5)
    assert len(spans) == 1
    assert 1.4 <= spans[0][0] <= 1.55
    assert 1.85 <= spans[0][1] <= 2.0


def test_jump_cuts_trim_silent_dead_air_but_preserve_loud_explosions() -> None:
    words = [
        WordTimestamp(word="Watch", start=0.5, end=1.2),
        WordTimestamp(word="this", start=1.5, end=2.2),
        WordTimestamp(word="boom", start=6.0, end=7.0),  # 3.8s gap between 2.2 and 6.0
        WordTimestamp(word="car", start=7.5, end=8.5),
        WordTimestamp(word="flying", start=9.0, end=10.5),
        WordTimestamp(word="over", start=11.0, end=12.5),
        WordTimestamp(word="the", start=13.0, end=14.2),
        WordTimestamp(word="crazy", start=14.8, end=16.5),
        WordTimestamp(word="bridge", start=17.0, end=18.8),
        WordTimestamp(word="ending", start=19.2, end=21.5),
    ]
    tr = AnnotatedTranscript(slice_id="s1", language="en", full_text="Watch this boom car flying over the crazy bridge ending", words=words)
    # Silent PCM -> trims the dead-air gap while keeping >= 15s total
    silent_pcm = np.zeros(24 * 16000, dtype=np.float32)
    kept_silent = compute_jump_cut_segments(24.0, 0.0, tr, pcm_16k=silent_pcm)
    assert len(kept_silent) >= 2
    assert sum(e - s for s, e in kept_silent) < 23.0

    # Loud explosion PCM -> refuses to cut any gap
    loud_pcm = np.full(24 * 16000, 0.25, dtype=np.float32)
    kept_loud = compute_jump_cut_segments(24.0, 0.0, tr, pcm_16k=loud_pcm)
    assert kept_loud == [(0.0, 24.0)]


def test_punch_in_windows_selected_from_peaks() -> None:
    tr = AnnotatedTranscript(
        slice_id="s1",
        language="en",
        full_text="test",
        non_verbal_events=[
            NonVerbalEvent(kind="shouting", start=14.0, end=15.0, intensity=0.9),
            NonVerbalEvent(kind="laughter", start=22.0, end=23.0, intensity=0.8),
        ],
    )
    wins = find_punch_in_windows(tr, start_trim=10.0, out_dur=24.0, kept_segments=None, freeze_ts=18.0)
    assert 1 <= len(wins) <= 2
    assert all(0.5 < s < e < 24.0 for s, e in wins)
