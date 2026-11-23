"""Post-Publish Analytics & Feedback Loop (`runs/studio.db` telemetry -> `context/performance_feedback.json`)."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.db import DEFAULT_DB_PATH, init_db, insert_telemetry
from src.miner import PROJECT_ROOT

FEEDBACK_JSON_PATH = PROJECT_ROOT / "context" / "performance_feedback.json"
VALID_PLATFORMS = {"youtube_shorts", "tiktok", "instagram_reels", "facebook_reels", "x", "youtube_longform"}


def record_publish_metrics(
    run_id: str,
    clip_id: str,
    platform: str,
    *,
    views: int,
    likes: int = 0,
    shares: int = 0,
    comments: int = 0,
    avg_view_pct: float = 0.0,
    swiped_away_pct: float = 0.0,
    published_url: str = "",
    streamer_id: str = "",
    hook_style: str = "",
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Persist real post-publish performance telemetry for a published clip."""
    plat = platform.strip().lower()
    if plat not in VALID_PLATFORMS:
        raise ValueError(f"Invalid platform {platform!r}; expected one of {sorted(VALID_PLATFORMS)}")
    payload: dict[str, Any] = {
        "clip_id": clip_id,
        "platform": plat,
        "views": max(0, int(views)),
        "likes": max(0, int(likes)),
        "shares": max(0, int(shares)),
        "comments": max(0, int(comments)),
        "avg_view_pct": round(max(0.0, float(avg_view_pct)), 2),
        "swiped_away_pct": round(max(0.0, min(100.0, float(swiped_away_pct))), 2),
        "engagement_rate_pct": round(((likes + shares + comments) / max(1, views)) * 100.0, 2),
        "published_url": published_url.strip(),
        "streamer_id": streamer_id.strip(),
        "hook_style": hook_style.strip(),
    }
    insert_telemetry(run_id, "publish_metrics", payload, db_path=db_path)
    return payload


def summarize_performance(
    db_path: Path = DEFAULT_DB_PATH,
    out_path: Path | None = FEEDBACK_JSON_PATH,
) -> dict[str, Any]:
    """Aggregate real post-publish metrics across all 5+ platforms and rank winning hooks/streamers."""
    init_db(db_path)
    conn = sqlite3.connect(str(db_path), timeout=15.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT ts, run_id, payload_json FROM telemetry WHERE event_type = 'publish_metrics' ORDER BY id ASC"
        ).fetchall()
    finally:
        conn.close()

    # Keep latest metric snapshot per (run_id, clip_id, platform)
    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    for r in rows:
        try:
            d = json.loads(r["payload_json"])
        except (ValueError, TypeError):
            continue
        key = (str(r["run_id"]), str(d.get("clip_id") or ""), str(d.get("platform") or ""))
        latest[key] = {**d, "run_id": str(r["run_id"]), "ts": str(r["ts"])}

    by_platform: dict[str, dict[str, Any]] = defaultdict(lambda: {"clips": 0, "views": 0, "avg_view_pct_sum": 0.0, "shares": 0})
    by_streamer: dict[str, dict[str, Any]] = defaultdict(lambda: {"clips": 0, "views": 0})
    by_hook: dict[str, dict[str, Any]] = defaultdict(lambda: {"clips": 0, "views": 0})

    records = list(latest.values())
    for rec in records:
        p = str(rec["platform"])
        v = int(rec.get("views") or 0)
        by_platform[p]["clips"] += 1
        by_platform[p]["views"] += v
        by_platform[p]["shares"] += int(rec.get("shares") or 0)
        by_platform[p]["avg_view_pct_sum"] += float(rec.get("avg_view_pct") or 0.0)
        sid = str(rec.get("streamer_id") or "unknown")
        by_streamer[sid]["clips"] += 1
        by_streamer[sid]["views"] += v
        hk = str(rec.get("hook_style") or "unspecified")
        by_hook[hk]["clips"] += 1
        by_hook[hk]["views"] += v

    platform_summary = {
        p: {
            "clips": st["clips"],
            "total_views": st["views"],
            "total_shares": st["shares"],
            "avg_view_pct": round(st["avg_view_pct_sum"] / max(1, st["clips"]), 2),
        }
        for p, st in sorted(by_platform.items())
    }
    top_clips = sorted(records, key=lambda x: int(x.get("views") or 0), reverse=True)[:5]

    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "tracked_posts": len(records),
        "total_views": sum(int(r.get("views") or 0) for r in records),
        "by_platform": platform_summary,
        "top_streamers": sorted(
            [{"streamer_id": k, **v} for k, v in by_streamer.items()],
            key=lambda x: x["views"],
            reverse=True,
        )[:5],
        "top_hooks": sorted(
            [{"hook_style": k, **v} for k, v in by_hook.items()],
            key=lambda x: x["views"],
            reverse=True,
        )[:5],
        "top_clips": top_clips,
    }
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report
