"""Unit tests for src/rag_engine.py: hybrid BM25 + sqlite-vec RRF, 6 gaps, and strict ?mode=ro&immutable=1 bridge."""

from __future__ import annotations

import hashlib
import math
import sqlite3
from pathlib import Path

from src.rag_engine import (
    EMBEDDING_DIM,
    build_curator_rag_context,
    format_rag_context,
    query_omni_rag_federated,
    query_rag,
    rebuild_rag_index,
)


def _deterministic_embed(texts: list[str]) -> list[list[float]]:
    """Fast unit-normalized 384-d bag-of-trigrams hash embedder for unit tests (zero network)."""
    out: list[list[float]] = []
    for txt in texts:
        vec = [0.0] * EMBEDDING_DIM
        low = txt.lower()
        for i in range(max(1, len(low) - 2)):
            gram = low[i : i + 3]
            h = int.from_bytes(hashlib.blake2b(gram.encode("utf-8"), digest_size=4).digest(), "little")
            vec[h % EMBEDDING_DIM] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        out.append([v / norm for v in vec])
    return out


def test_rag_hybrid_bm25_vec_rrf_and_idempotency(tmp_path: Path) -> None:
    db_file = tmp_path / "rag_test.db"
    sample_chunks = [
        {
            "chunk_id": "design_ctr:1",
            "source_path": "docs/design_bible.md",
            "domain": "design_ctr",
            "heading": "4-Network Universal Safe Box & APCA",
            "content": "Keep 9:16 cover headlines inside x=90..990, y=420..1250 with APCA Lc >= 75 and Squint Test 160px.",
        },
        {
            "chunk_id": "gta6_streamers:1",
            "source_path": "context/streamer_profiles.json",
            "domain": "gta6_streamers",
            "heading": "Streamer Profile: Jynxzi",
            "content": "Jynxzi explosive comedy rage reactions; preserve 0.25s Synchresis silence right before scream.",
        },
        {
            "chunk_id": "episodic_memory:1",
            "source_path": "runs/studio.db#clips",
            "domain": "episodic_memory",
            "heading": "Episodic Clip WINNER: jynxzi clip_02",
            "content": "Episodic Clip WINNER critic_overall=0.91 high retention opening chase hook in GTA VI.",
        },
    ]

    r1 = rebuild_rag_index(db_path=db_file, chunks_override=sample_chunks, embed_fn=_deterministic_embed)
    assert r1["total_chunks"] == 3
    assert r1["inserted"] == 3
    assert r1["vec_enabled"] is True
    assert r1["vec_chunks"] == 3

    # Second rebuild must be idempotent (0 newly inserted/embedded)
    r2 = rebuild_rag_index(db_path=db_file, chunks_override=sample_chunks, embed_fn=_deterministic_embed)
    assert r2["inserted"] == 0
    assert r2["unchanged"] == 3
    assert r2["embedded_now"] == 0

    hits = query_rag("APCA Safe Box 160px", domain="design_ctr", top_k=3, db_path=db_file, embed_fn=_deterministic_embed)
    assert len(hits) == 1
    assert hits[0]["chunk_id"] == "design_ctr:1"
    assert hits[0]["bm25_rank"] == 1
    assert hits[0]["vec_rank"] == 1

    ctx = build_curator_rag_context("jynxzi", "Synchresis scream retention", max_tokens=550, db_path=db_file)
    assert "Jynxzi" in ctx
    assert len(ctx) <= 550 * 4 + 16


def test_federated_omni_rag_strict_readonly_no_wal_shm_created(tmp_path: Path) -> None:
    """Verify federated bridge opens with ?mode=ro&immutable=1 without creating -wal/-shm files."""
    fake_omni_dir = tmp_path / "omni_cache"
    fake_omni_dir.mkdir()
    lib_db = fake_omni_dir / "rag_library.sqlite"

    con = sqlite3.connect(str(lib_db))
    con.executescript(
        """
        CREATE TABLE docs (doc_id INTEGER PRIMARY KEY, rel_path TEXT, title TEXT, partition TEXT, category TEXT);
        CREATE TABLE chunks (chunk_id INTEGER PRIMARY KEY, doc_id INTEGER, chunk_uid TEXT, heading_path TEXT, text TEXT);
        CREATE VIRTUAL TABLE chunks_fts USING fts5(heading_path, text, content='chunks', content_rowid='chunk_id');
        INSERT INTO docs VALUES (1, 'books/murch.md', 'In the Blink of an Eye - Walter Murch', 'library', 'editing');
        INSERT INTO chunks VALUES (1, 1, 'murch-001', 'Rule of Six', 'Emotion 51%, Story 23%, Rhythm 10%, Eye-Trace 7% by Walter Murch.');
        INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild');
        """
    )
    con.commit()
    con.close()

    mtime_before = lib_db.stat().st_mtime_ns
    hits = query_omni_rag_federated("Walter Murch Rule of Six Emotion", limit=3, omni_dir=fake_omni_dir)
    assert len(hits) == 1
    assert hits[0]["source"] == "omni_rag_library"
    assert "Emotion 51%" in hits[0]["content"]
    # Strictly read-only: no -wal or -shm created and mtime unchanged
    assert not (fake_omni_dir / "rag_library.sqlite-wal").exists()
    assert not (fake_omni_dir / "rag_library.sqlite-shm").exists()
    assert lib_db.stat().st_mtime_ns == mtime_before

    # Graceful silent degradation if directory does not exist
    assert query_omni_rag_federated("Murch", omni_dir=tmp_path / "does_not_exist") == []


def test_format_rag_context_respects_token_budget() -> None:
    hits = [
        {"domain": "design_ctr", "source": "studio_local", "heading": f"H{i}", "content": "word " * 120}
        for i in range(10)
    ]
    out = format_rag_context(hits, max_tokens=150)
    assert len(out) <= 150 * 4 + 10
