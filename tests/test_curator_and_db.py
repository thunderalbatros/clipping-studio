"""Unit tests for curator deterministic anti-meta-talk gate, SQLite WAL/FTS5, daily USD cap, and resource_guard."""

from __future__ import annotations

from pathlib import Path

from src.curator import _checked_by_code
from src.db import (
    get_daily_llm_spend_usd,
    init_db,
    insert_telemetry,
    inspect_db_summary,
    search_fts,
    upsert_slice,
    upsert_transcript_fts,
    upsert_vod,
)
from src.rag_engine import _split_markdown_chunks, query_rag, rebuild_rag_index
from src.resource_guard import heavy_local_allowed, onnx_threads, snapshot
from src.schemas import AnnotatedTranscript, EditPlan, WordTimestamp


def test_checked_by_code_rejects_gifted_meta_talk() -> None:
    words = [
        WordTimestamp(word="Thank", start=1.0, end=1.2),
        WordTimestamp(word="you", start=1.3, end=1.4),
        WordTimestamp(word="for", start=1.5, end=1.6),
        WordTimestamp(word="the", start=1.7, end=1.8),
        WordTimestamp(word="10", start=1.9, end=2.1),
        WordTimestamp(word="gifted", start=2.2, end=2.6),
        WordTimestamp(word="subs", start=2.7, end=3.0),
    ]
    tr = AnnotatedTranscript(slice_id="s1", language="en", full_text="Thank you for the 10 gifted subs", words=words)
    plan = EditPlan(
        clip_id="clip_01",
        slice_id="s1",
        source_url="https://www.twitch.tv/videos/1",
        start_trim=0.0,
        end_trim=22.0,
        scene_type="active_gameplay",
        visual_hook_score=0.8,
        hook_headline="Sub shoutout",
        self_contained_score=80,
        rationale="test",
    )
    ok, issues = _checked_by_code(plan, tr, max_dur=40.0)
    assert ok is False
    assert any("gifted" in i.lower() for i in issues)


def test_sqlite_wal_fts5_and_daily_llm_spend(tmp_path: Path) -> None:
    db_file = tmp_path / "test_studio.db"
    init_db(db_file)
    upsert_vod("https://kick.com/xqc", "kick", "xqc", title="GTA VI", duration_sec=3600.0, run_id="r1", db_path=db_file)
    upsert_slice(
        run_id="r1",
        slice_id="slice_01",
        source_url="https://kick.com/xqc",
        platform="kick",
        start_sec=0.0,
        end_sec=40.0,
        score=9.2,
        video_path="runs/r1/slice_01.mp4",
        signal_source="kick_clips_rank",
        db_path=db_file,
    )
    upsert_transcript_fts("r1", "slice_01", "See you in Valhalla Jason!", "Jason Duval", db_path=db_file)
    insert_telemetry("llm_router", "llm_usage", {"est_cost_usd": 0.0125}, db_path=db_file)
    insert_telemetry("llm_router", "llm_usage", {"est_cost_usd": 0.0075}, db_path=db_file)

    summary = inspect_db_summary(db_file)
    assert str(summary["journal_mode"]).lower() == "wal"
    assert summary["counts"]["vods"] == 1
    assert summary["counts"]["slices"] == 1
    hits = search_fts("Valhalla", db_path=db_file)
    assert len(hits) == 1
    assert hits[0]["lore_entities"] == "Jason Duval"
    assert get_daily_llm_spend_usd(db_path=db_file) == 0.02


def test_resource_guard_snapshot_and_threads() -> None:
    snap = snapshot()
    assert snap["logical_cores"] >= 1
    assert snap["total_ram_mb"] > 0
    assert 1 <= onnx_threads() <= 4
    allowed, reason, _ = heavy_local_allowed(min_free_ram_mb=1, min_free_disk_gb=0.1)
    assert allowed is True
    assert reason == "resources healthy"


def test_knowledge_fts_ingestion_idempotency_and_bm25_search(tmp_path: Path) -> None:
    db_file = tmp_path / "knowledge.db"
    md_file = tmp_path / "algos.md"
    md_file.write_text(
        "## YouTube Shorts MMoE\n"
        "Viewed vs Swiped Away above 70 percent enters viral distribution with MMoE satisfaction models.\n\n"
        "## X Phoenix Heavy Ranker\n"
        "Reply with author engagement carries 75.0 weight while external links suffer reach penalties.\n",
        encoding="utf-8",
    )
    chunks = _split_markdown_chunks(md_file, default_domain="shortform_craft")
    r1 = rebuild_rag_index(db_path=db_file, chunks_override=chunks, skip_vectors=True)
    assert r1["inserted"] == 2 and r1["unchanged"] == 0
    r2 = rebuild_rag_index(db_path=db_file, chunks_override=chunks, skip_vectors=True)
    assert r2["inserted"] == 0 and r2["unchanged"] == 2
    hits = query_rag("MMoE Swiped", domain="shortform_craft", db_path=db_file, use_vectors=False)
    assert len(hits) >= 1
    assert "YouTube Shorts" in hits[0]["heading"]

