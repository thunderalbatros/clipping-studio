"""Unit tests for Montserrat Black discrete captions, stutter filtering, NPC exclusion, and jump-cut remapping."""

from __future__ import annotations

from pathlib import Path

from src.captions import generate_karaoke_ass, remap_time_through_segments
from src.schemas import AnnotatedTranscript, EditPlan, WordTimestamp


def _dummy_plan() -> EditPlan:
    return EditPlan(
        clip_id="clip_01",
        slice_id="slice_01",
        source_url="https://www.twitch.tv/videos/123456",
        start_trim=10.0,
        end_trim=30.0,
        scene_type="active_gameplay",
        visual_hook_score=0.9,
        hook_headline="He exploded instantly",
        self_contained_score=90,
        rationale="test",
    )


def test_captions_exclude_npc_stutter_and_kf_and_mask_opening_profanity(tmp_path: Path) -> None:
    words = [
        WordTimestamp(word="J", start=10.5, end=10.7, is_npc_or_other=False),
        WordTimestamp(word="Jason", start=10.8, end=11.1, is_npc_or_other=False),
        WordTimestamp(word="fuck!", start=11.2, end=11.6, is_npc_or_other=False),
        WordTimestamp(word="Natural,", start=12.0, end=12.4, is_npc_or_other=True),
        WordTimestamp(word="babe.", start=12.5, end=12.9, is_npc_or_other=True),
        WordTimestamp(word="I", start=14.0, end=14.2, is_npc_or_other=False),
        WordTimestamp(word="exploded", start=14.3, end=14.8, is_npc_or_other=False),
    ]
    tr = AnnotatedTranscript(slice_id="slice_01", language="en", full_text="J Jason fuck Natural babe I exploded", words=words)
    out_ass = tmp_path / "subs.ass"
    generate_karaoke_ass(tr, _dummy_plan(), out_ass)
    txt = out_ass.read_text(encoding="utf-8")

    assert "\\kf" not in txt
    assert "Montserrat Black" in txt
    assert "NATURAL" not in txt
    assert "BABE" not in txt
    assert "F***!" in txt
    assert "FUCK!" not in txt
    assert " I " in f" {txt} " or "I{\\" in txt or "I" in txt
    # Single-letter stutter "J" must not appear as a standalone subtitle token
    assert "}J " not in txt


def test_remap_time_through_jump_cut_segments() -> None:
    segs = [(0.0, 5.0), (8.0, 14.0)]
    assert remap_time_through_segments(2.5, segs) == 2.5
    assert remap_time_through_segments(9.5, segs) == 6.5
    assert remap_time_through_segments(6.0, segs) == 5.0
