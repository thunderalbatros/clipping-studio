"""Storage hygiene & garbage collector (`python -m src.cli gc`) for `runs/`.

Purges temporary/intermediate files (WAVs, loose QA frames, pre-libass temp renders,
old QA heatmaps, and MP4s of PUBLISHED/REJECTED clips) while strictly preserving:
- `runs/studio.db` (+ `-wal` / `-shm`)
- `models/` (never touched)
- Clips in `READY_HITL` state (and any active approved master MP4/cover/packaging not marked PUBLISHED/REJECTED)
"""

from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

from src.db import DEFAULT_DB_PATH, PROJECT_ROOT, init_db

RUNS_DIR = PROJECT_ROOT / "runs"

PROTECTED_ROOT_FILES = frozenset({"studio.db", "studio.db-wal", "studio.db-shm", "telemetry.jsonl"})
INTERMEDIATE_FILE_SUFFIXES = (".wav", ".tmp", ".part")
INTERMEDIATE_NAME_TOKENS = (
    "_pre_subtitles",
    "_clean_video",
    "_saliency_heatmap",
    "_crop_preview",
    "_squint_160px",
    "_safezones",
)
INTERMEDIATE_DIR_TOKENS = ("_qa_frames", "_senior_qa", "_frames_tmp", "_work_tmp")


def _load_clip_statuses(db_path: Path) -> tuple[set[Path], set[Path]]:
    """Return `(protected_mp4s, disposable_mp4s)` resolved from `studio.db` clips table."""
    protected: set[Path] = set()
    disposable: set[Path] = set()
    if not db_path.exists():
        return protected, disposable

    init_db(db_path)
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(clips)").fetchall()}
        has_status = "status" in cols
        query = (
            "SELECT output_mp4, media_check_passed, status FROM clips"
            if has_status
            else "SELECT output_mp4, media_check_passed FROM clips"
        )
        for row in conn.execute(query).fetchall():
            raw_mp4 = str(row["output_mp4"] or "").strip()
            if not raw_mp4:
                continue
            p = Path(raw_mp4)
            if not p.is_absolute():
                p = (PROJECT_ROOT / p).resolve(strict=False)
            else:
                p = p.resolve(strict=False)

            status = str(row["status"] or "").upper() if has_status else ""
            passed = bool(row["media_check_passed"])
            if status in {"PUBLISHED", "REJECTED"}:
                disposable.add(p)
            elif status == "READY_HITL" or passed:
                protected.add(p)
            else:
                disposable.add(p)
    finally:
        conn.close()
    return protected, disposable


def _dir_size_bytes(path: Path) -> int:
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            try:
                total += child.stat().st_size
            except OSError:
                continue
    return total


def run_storage_gc(
    runs_dir: Path = RUNS_DIR,
    db_path: Path = DEFAULT_DB_PATH,
    ttl_hours: float = 72.0,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Scan and optionally purge disposable intermediate artifacts inside `runs_dir`."""
    runs_root = runs_dir.resolve(strict=False)
    if not runs_root.exists():
        return {
            "dry_run": dry_run,
            "ttl_hours": ttl_hours,
            "runs_dir": str(runs_root),
            "protected_clips": 0,
            "candidates_count": 0,
            "deleted_count": 0,
            "reclaimed_mb": 0.0,
            "items": [],
        }

    protected_mp4s, disposable_mp4s = _load_clip_statuses(db_path)
    now_ts = time.time()
    ttl_sec = max(0.0, float(ttl_hours)) * 3600.0

    candidates: list[dict[str, Any]] = []
    scheduled_dirs: set[Path] = set()

    # 1. Identify disposable QA/tmp subdirectories first
    for entry in sorted(runs_root.rglob("*")):
        if not entry.is_dir():
            continue
        name_l = entry.name.lower()
        if any(tok in name_l for tok in INTERMEDIATE_DIR_TOKENS):
            try:
                age_sec = max(0.0, now_ts - entry.stat().st_mtime)
            except OSError:
                continue
            if age_sec >= ttl_sec:
                size_b = _dir_size_bytes(entry)
                scheduled_dirs.add(entry.resolve(strict=False))
                candidates.append(
                    {
                        "path": str(entry),
                        "kind": "dir",
                        "reason": "qa_or_tmp_dir_ttl",
                        "size_bytes": size_b,
                        "age_hours": round(age_sec / 3600.0, 2),
                    }
                )

    # 2. Identify disposable files
    for entry in sorted(runs_root.rglob("*")):
        if not entry.is_file():
            continue
        res = entry.resolve(strict=False)

        # Never touch studio.db / telemetry at runs root
        if res.parent == runs_root and res.name in PROTECTED_ROOT_FILES:
            continue
        # Skip files already inside a directory scheduled for removal
        if any(res.is_relative_to(d) for d in scheduled_dirs):
            continue
        # Strictly preserve READY_HITL / approved clips
        if res in protected_mp4s:
            continue

        try:
            st = entry.stat()
        except OSError:
            continue
        age_sec = max(0.0, now_ts - st.st_mtime)
        name_l = entry.name.lower()
        suffix_l = entry.suffix.lower()

        reason: str | None = None
        if res in disposable_mp4s:
            reason = "published_or_rejected_clip_mp4"
        elif suffix_l in INTERMEDIATE_FILE_SUFFIXES and age_sec >= ttl_sec:
            reason = f"intermediate_{suffix_l.lstrip('.')}_ttl"
        elif any(tok in name_l for tok in INTERMEDIATE_NAME_TOKENS) and age_sec >= ttl_sec:
            reason = "intermediate_render_or_qa_ttl"

        if reason:
            candidates.append(
                {
                    "path": str(entry),
                    "kind": "file",
                    "reason": reason,
                    "size_bytes": st.st_size,
                    "age_hours": round(age_sec / 3600.0, 2),
                }
            )

    deleted_count = 0
    reclaimed_bytes = 0
    for item in candidates:
        reclaimed_bytes += int(item["size_bytes"])
        if not dry_run:
            p = Path(str(item["path"]))
            try:
                if item["kind"] == "dir" and p.exists():
                    shutil.rmtree(p)
                    deleted_count += 1
                elif item["kind"] == "file" and p.exists():
                    p.unlink()
                    deleted_count += 1
            except OSError:
                continue

    return {
        "dry_run": dry_run,
        "ttl_hours": ttl_hours,
        "runs_dir": str(runs_root),
        "protected_clips": len(protected_mp4s),
        "candidates_count": len(candidates),
        "deleted_count": deleted_count,
        "reclaimed_mb": round(reclaimed_bytes / (1024 * 1024), 2),
        "items": candidates[:50],
    }
