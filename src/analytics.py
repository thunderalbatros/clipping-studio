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
from src.schemas import ClipPackaging

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
    vvsa_pct: float | None = None,
    sends_per_reach: float = 0.0,
    cpm_usd: float = 0.0,
    published_url: str = "",
    streamer_id: str = "",
    hook_style: str = "",
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Persist real post-publish performance telemetry (VVSA, APV%, Sends/Reach, views, CPM) -> studio.db."""
    plat = platform.strip().lower()
    if plat not in VALID_PLATFORMS:
        raise ValueError(f"Invalid platform {platform!r}; expected one of {sorted(VALID_PLATFORMS)}")
    swiped = round(max(0.0, min(100.0, float(swiped_away_pct))), 2)
    eff_vvsa = round(max(0.0, min(100.0, float(vvsa_pct))), 2) if vvsa_pct is not None else round(100.0 - swiped, 2)
    eff_sends = round(max(0.0, float(sends_per_reach)), 4) if sends_per_reach > 0 else round(shares / max(1, views), 4)
    payload: dict[str, Any] = {
        "clip_id": clip_id,
        "platform": plat,
        "views": max(0, int(views)),
        "likes": max(0, int(likes)),
        "shares": max(0, int(shares)),
        "comments": max(0, int(comments)),
        "avg_view_pct": round(max(0.0, float(avg_view_pct)), 2),
        "swiped_away_pct": swiped,
        "vvsa_pct": eff_vvsa,
        "sends_per_reach": eff_sends,
        "cpm_usd": round(max(0.0, float(cpm_usd)), 2),
        "engagement_rate_pct": round(((likes + shares + comments) / max(1, views)) * 100.0, 2),
        "published_url": published_url.strip(),
        "streamer_id": streamer_id.strip(),
        "hook_style": hook_style.strip(),
    }
    insert_telemetry(run_id, "publish_metrics", payload, db_path=db_path)
    return payload


def emit_hitl_kit_5_networks(
    pkg: ClipPackaging,
    clip_mp4: Path,
    out_dir: Path,
) -> dict[str, Any]:
    """Emit ready-to-copy/paste HITL Publishing Kit (.md + .json) for all 5 networks."""
    out_dir.mkdir(parents=True, exist_ok=True)
    tags_str = " ".join(pkg.shorts_hashtags)
    ab_list = list(pkg.ab_hook_variants) or [pkg.suggested_on_screen_hook, "WAIT FOR IT"]
    author_seed = pkg.author_reply_seed.strip() or "Would you have survived that or instant panic?"
    fb_copy = pkg.facebook_reels_caption.strip() or f"{pkg.shorts_title} — who else did this on day one?"
    x_copy = pkg.x_post_text.strip() or f"{pkg.shorts_title} #GTA6"

    kit: dict[str, Any] = {
        "clip_id": pkg.clip_id,
        "video_mp4": str(clip_mp4),
        "cover_9_16_path": pkg.cover_9_16_path,
        "cover_clean_path": pkg.cover_clean_path,
        "cover_timestamp_ms": pkg.cover_timestamp_ms,
        "cover_hook_text": pkg.suggested_on_screen_hook,
        "ab_hook_variants": ab_list,
        "author_reply_seed": author_seed,
        "audio_policy": pkg.audio_policy,
        "dmca_advisory": pkg.dmca_advisory,
        "sound_campaign_climax_ms": pkg.sound_campaign_climax_ms,
        "sound_sync_note": pkg.sound_sync_note,
        "bounty_safe": pkg.bounty_safe,
        "networks": {
            "youtube_shorts": {
                "title": pkg.shorts_title,
                "hashtags": pkg.shorts_hashtags,
                "description": f"{pkg.shorts_title}\n\n{tags_str}",
                "cover_timestamp_ms": pkg.cover_timestamp_ms,
                "cover_jpg": pkg.cover_9_16_path,
            },
            "tiktok": {
                "caption": f"{pkg.tiktok_caption} {tags_str}".strip(),
                "cover_jpg": pkg.cover_9_16_path,
                "ab_hook_variants": ab_list,
                "sound_sync_note": pkg.sound_sync_note,
            },
            "instagram_reels": {
                "caption": f"{pkg.reels_caption}\n\n{tags_str}".strip(),
                "cover_jpg": pkg.cover_9_16_path,
                "trial_reels_ab_hooks": ab_list,
            },
            "facebook_reels": {
                "caption": f"{fb_copy}\n\n{tags_str}".strip(),
                "cover_jpg": pkg.cover_9_16_path,
            },
            "x": {
                "post_text": x_copy,
                "author_reply_seed": author_seed,
                "rule": "Post native MP4 with ZERO external links in main post; reply manually with author_reply_seed for 75x boost.",
            },
        },
    }

    kit_json = out_dir / f"{pkg.clip_id}_hitl_kit_5_networks.json"
    kit_md = out_dir / f"{pkg.clip_id}_hitl_kit_5_networks.md"
    kit_json.write_text(json.dumps(kit, indent=2, ensure_ascii=False), encoding="utf-8")

    md_lines = [
        f"# HITL Publishing Kit (5 Redes) — `{pkg.clip_id}`",
        "",
        f"- **Master MP4:** `{clip_mp4}`",
        f"- **Cover 9:16:** `{pkg.cover_9_16_path}` (`cover_timestamp_ms={pkg.cover_timestamp_ms}`)",
        f"- **Cover Hook (Complementary):** `{pkg.suggested_on_screen_hook}`",
        f"- **A/B Hook Variants:** {', '.join(f'`{h}`' for h in ab_list)}",
        f"- **Audio Policy:** `{pkg.audio_policy}` | **Bounty Safe:** `{pkg.bounty_safe}`",
    ]
    if pkg.sound_sync_note:
        md_lines.append(f"- **Sound Campaign Sync:** {pkg.sound_sync_note}")
    if pkg.dmca_advisory:
        md_lines.append(f"- **DMCA Advisory:** {pkg.dmca_advisory}")
    md_lines.extend(
        [
            "",
            "## 1. YouTube Shorts",
            f"- **Title:** {pkg.shorts_title}",
            f"- **Tags:** {tags_str}",
            f"- **Cover Timestamp:** `{pkg.cover_timestamp_ms} ms`",
            "",
            "## 2. TikTok",
            f"- **Caption:** {pkg.tiktok_caption} {tags_str}",
            "",
            "## 3. Instagram Reels / Trial Reels",
            f"- **Caption:** {pkg.reels_caption} {tags_str}",
            "",
            "## 4. Facebook Reels",
            f"- **Caption:** {fb_copy} {tags_str}",
            "",
            "## 5. X (Native Video + 75x Author Reply)",
            f"- **Main Post:** {x_copy}",
            f"- **Author Reply Seed (manual):** {author_seed}",
            "",
        ]
    )
    kit_md.write_text("\n".join(md_lines), encoding="utf-8")
    kit["kit_json_path"] = str(kit_json)
    kit["kit_md_path"] = str(kit_md)
    return kit


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
