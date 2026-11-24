"""Daily Viral Trend Radar (YouTube Data API v3 + Kick Clips via curl_cffi) -> context/golden_references.json (Hito 2.0)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from curl_cffi import requests as curl_requests
from dotenv import load_dotenv

from src.db import insert_telemetry
from src.llm_router import reset_cached_prefix
from src.miner import PROJECT_ROOT, fetch_kick_gta6_clips, load_whitelist
from src.runtime_paths import parse_iso_duration_sec as _parse_iso_duration_sec

GOLDEN_REFS_PATH = PROJECT_ROOT / "context" / "golden_references.json"
LIVING_BRIEF_PATH = PROJECT_ROOT / "context" / "gta6_living_brief.md"


def _infer_hook_style(title: str) -> tuple[str, str]:
    t = title.lower()
    if any(k in t for k in ("secret", "easter egg", "hidden", "detail", "found", "area 51", "dexter")):
        return "Secret / Lore Discovery", "Curiosity gap reveal ('Rockstar hid THIS...')"
    if any(k in t for k in ("bug", "glitch", "physics", "broke", "crash", "face plant", "insane")):
        return "Physics Chaos / Viral Bug", "Instant action cold-start -> disbelief reaction"
    if any(k in t for k in ("police", "cop", "star", "heist", "chase", "helicopter", "escape")):
        return "High-Stakes Action / Chase", "In-media-res adrenaline opening"
    if any(k in t for k in ("funny", "lol", "react", "npc", "cat", "alligator", "gator")):
        return "Comedic Chaos / NPC AI", "Setup -> sharp unexpected punchline"
    return "Gameplay Moment", "Direct action-reaction hook"


def fetch_youtube_gta6_shorts_trends(hours_back: int = 48, max_results: int = 12) -> list[dict[str, Any]]:
    """Query YouTube Data API v3 for #GTA6 #Shorts in the last 24-48h with views_per_hour & like_ratio."""
    load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=True)
    api_key = (os.getenv("YOUTUBE_API_KEY") or "").strip()
    if not api_key:
        print("[TRENDS] YOUTUBE_API_KEY missing; skipping YouTube Data API v3.")
        return []

    since_iso = (datetime.now(UTC) - timedelta(hours=hours_back)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sr = curl_requests.get(
        "https://www.googleapis.com/youtube/v3/search",
        params={
            "part": "snippet",
            "q": "GTA 6 gameplay #Shorts",
            "type": "video",
            "videoDuration": "short",
            "order": "viewCount",
            "publishedAfter": since_iso,
            "relevanceLanguage": "en",
            "maxResults": max_results,
            "key": api_key,
        },
        timeout=15,
    )
    if sr.status_code != 200:
        print(f"[TRENDS] YouTube search HTTP {sr.status_code}: {sr.text[:140]}")
        return []

    items = (sr.json() or {}).get("items") or []
    vid_ids = [it["id"]["videoId"] for it in items if (it.get("id") or {}).get("videoId")]
    if not vid_ids:
        return []

    vr = curl_requests.get(
        "https://www.googleapis.com/youtube/v3/videos",
        params={
            "part": "snippet,statistics,contentDetails",
            "id": ",".join(vid_ids),
            "key": api_key,
        },
        timeout=15,
    )
    if vr.status_code != 200:
        return []

    now = datetime.now(UTC)
    refs: list[dict[str, Any]] = []
    for v in (vr.json() or {}).get("items") or []:
        snip = v.get("snippet") or {}
        stats = v.get("statistics") or {}
        cdet = v.get("contentDetails") or {}
        title = str(snip.get("title") or "").strip()
        views = int(stats.get("viewCount") or 0)
        likes = int(stats.get("likeCount") or 0)
        dur = _parse_iso_duration_sec(str(cdet.get("duration") or ""))
        pub = str(snip.get("publishedAt") or "")
        age_h = 24.0
        if pub:
            try:
                dt = datetime.fromisoformat(pub)
                age_h = max(0.5, (now - dt).total_seconds() / 3600.0)
            except ValueError:
                pass
        vph = round(views / age_h, 1)
        like_ratio = round(likes / max(1, views), 4)
        topic, hook_style = _infer_hook_style(title)
        refs.append({
            "platform": "youtube_shorts",
            "source_id": str(v.get("id") or ""),
            "channel": str(snip.get("channelTitle") or ""),
            "title": title,
            "url": f"https://www.youtube.com/shorts/{v.get('id')}",
            "duration_sec": dur,
            "views": views,
            "views_per_hour": vph,
            "like_ratio": like_ratio,
            "topic": topic,
            "title_pattern": title[:55],
            "hook_style": hook_style,
        })
    refs.sort(key=lambda r: r["views_per_hour"], reverse=True)
    return refs


def fetch_kick_whitelist_trends(limit_per_streamer: int = 5) -> list[dict[str, Any]]:
    """Query Kick Clips API via curl_cffi across Kick streamers in config/streamers.yaml."""
    wl = load_whitelist()
    refs: list[dict[str, Any]] = []
    for sid, cfg in wl.items():
        if "kick" not in (cfg.get("platforms") or []):
            continue
        slug = str(cfg.get("kick_slug") or sid)
        clips = fetch_kick_gta6_clips(slug, time_window="week", limit=limit_per_streamer)
        for c in clips:
            topic, hook_style = _infer_hook_style(str(c["title"]))
            refs.append({
                "platform": "kick_clips",
                "source_id": str(c["id"]),
                "channel": slug,
                "title": str(c["title"]),
                "url": str(c["page_url"]),
                "duration_sec": float(c["duration"]),
                "views": int(c["views"]),
                "views_per_hour": float(c["views_per_hour"]),
                "like_ratio": None,
                "topic": topic,
                "title_pattern": str(c["title"])[:55],
                "hook_style": hook_style,
            })
    refs.sort(key=lambda r: r["views_per_hour"], reverse=True)
    return refs


def _refresh_living_brief_radar(top_refs: list[dict[str, Any]]) -> None:
    if not LIVING_BRIEF_PATH.exists() or not top_refs:
        return
    base = LIVING_BRIEF_PATH.read_text(encoding="utf-8")
    marker = "## Bloque D — Radar Automático 48h (Golden References)"
    if marker in base:
        base = base.split(marker)[0].rstrip()
    lines = [
        base,
        "",
        marker,
        f"- Actualizado: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
    ]
    for r in top_refs[:5]:
        lines.append(
            f"- [{r['platform']}] ({r['views_per_hour']} vph, {int(r['duration_sec'])}s) "
            f"{r['channel']}: {r['title']} -> {r['hook_style']}"
        )
    LIVING_BRIEF_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def fetch_social_network_outliers(limit_per_network: int = 3) -> list[dict[str, Any]]:
    """Discover viral GTA VI short-form signals on TikTok, IG Reels, FB Reels & X via Serper.dev."""
    api_key = (os.getenv("SERPER_API_KEY") or "").strip()
    if not api_key:
        return []
    queries = [
        ("tiktok", 'site:tiktok.com ("GTA 6" OR "GTA VI") (gameplay OR clip OR funny OR viral)'),
        ("instagram_reels", 'site:instagram.com/reel ("GTA 6" OR "GTA VI") gameplay'),
        ("facebook_reels", 'site:facebook.com/reel ("GTA 6" OR "GTA VI") gameplay'),
        ("x", 'site:x.com ("GTA 6" OR "GTA VI") (clip OR gameplay OR bug OR reaction)'),
    ]
    refs: list[dict[str, Any]] = []
    for platform, q in queries:
        try:
            resp = curl_requests.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                json={"q": q, "num": limit_per_network, "tbs": "qdr:w"},
                timeout=15,
            )
            if resp.status_code != 200:
                continue
            for idx, item in enumerate((resp.json() or {}).get("organic") or []):
                title = str(item.get("title") or "").strip()
                url = str(item.get("link") or "").strip()
                if not title or not url:
                    continue
                topic, hook_style = _infer_hook_style(title)
                refs.append({
                    "platform": platform,
                    "source_id": f"{platform}_{idx}",
                    "channel": str(item.get("displayLink") or platform),
                    "title": title,
                    "url": url,
                    "duration_sec": 30.0,
                    "views": 0,
                    "views_per_hour": 0.0,
                    "like_ratio": None,
                    "topic": topic,
                    "title_pattern": title[:55],
                    "hook_style": hook_style,
                })
        except Exception:
            continue
    return refs


def run_trends_radar(
    top_n: int = 10,
    all_networks: bool = True,
    refresh_style_dna: bool = False,
) -> dict[str, Any]:
    """Refresh context/golden_references.json across YT Shorts, Kick, TikTok, IG Reels, FB Reels & X."""
    load_dotenv(override=True)
    yt_refs = fetch_youtube_gta6_shorts_trends(hours_back=48, max_results=12)
    kick_refs = fetch_kick_whitelist_trends(limit_per_streamer=5)
    social_refs = fetch_social_network_outliers(limit_per_network=3) if all_networks else []

    # Ensure balanced representation across YouTube Shorts, Kick clips, and social networks
    combined = yt_refs[:5] + kick_refs[:3] + social_refs[:4]
    if len(combined) < top_n:
        seen = {r["url"] for r in combined}
        for r in sorted(yt_refs + kick_refs, key=lambda x: x["views_per_hour"], reverse=True) + social_refs:
            if r["url"] not in seen:
                combined.append(r)
                seen.add(r["url"])
            if len(combined) >= top_n:
                break

    networks_covered = sorted({r["platform"] for r in combined[:top_n]})
    payload: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "sources": {
            "youtube_shorts_fetched": len(yt_refs),
            "kick_clips_fetched": len(kick_refs),
            "social_networks_fetched": len(social_refs),
            "networks_covered": networks_covered,
        },
        "golden_references": combined[:top_n],
    }
    GOLDEN_REFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_REFS_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    _refresh_living_brief_radar(payload["golden_references"])

    if refresh_style_dna:
        from src.style_analyzer import analyze_video_style, download_and_analyze_url, synthesize_style_dna

        samples: list[dict[str, Any]] = []
        # Analyze top YouTube Shorts URLs (max 2 to stay fast) + any local master clips
        for ref in yt_refs[:2]:
            dna = download_and_analyze_url(ref["url"], max_sec=40)
            if dna:
                samples.append(dna)
        for local_mp4 in sorted((PROJECT_ROOT / "runs").glob("*/*master_9_16.mp4"))[:2]:
            try:
                samples.append(analyze_video_style(local_mp4, label=local_mp4.name))
            except Exception:
                continue
        payload["style_dna"] = synthesize_style_dna(samples)

    reset_cached_prefix()
    insert_telemetry("trends_radar", "trends_refreshed", payload["sources"])
    return payload
