"""Scene-Match Intelligence Researcher (v3.5 — 2-Level Agnostic Viral Reference Deconstruction + SQLite Cache).

Architecture:
- Level 1 (Exact Scene / Mission Match): builds targeted queries from game + scene/transcript/lore over
  YouTube Shorts (Data API v3 + yt-dlp Deno fallback) and Kick Clips (curl_cffi; zero TikTok/IG mass scraping),
  selects top 3-5 real viral short-form outliers (<=60s), deconstructs each deterministically
  (PySceneDetect + FFmpeg + UltraFace + optional parakeet ASR) and multimodally (gemini-3.8-flash),
  and automatically purges every temporary reference MP4 on exit.
- Level 2 (Archetype Fallback): when <2 exact viral matches exist (live/unseen scenes), falls back cleanly
  to `context/viral_style_dna.json` + local RAG (`shortform_craft`) without blocking.
- Cache: persists structured Pydantic v2 `SceneBlueprint` into `runs/studio.db` table `scene_blueprints`
  for instant reuse across streamers playing the same scene/mission.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import statistics
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from curl_cffi import requests as curl_requests
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, ConfigDict, Field

from src.db import (
    DEFAULT_DB_PATH,
    _connect,
    get_scene_blueprint,
    init_db,
    insert_telemetry,
    upsert_scene_blueprint,
)
from src.llm_router import build_cached_system_instruction, generate_structured, wrap_untrusted_source_data
from src.miner import PROJECT_ROOT, fetch_kick_gta6_clips, load_whitelist
from src.rag_engine import query_rag
from src.renderer import detect_scene_cuts_pyscenedetect
from src.runtime_paths import (
    ensure_media_path,
    find_deno_executable,
    format_hms,
    resolve_binary_str,
)
from src.runtime_paths import (
    parse_iso_duration_sec as _parse_iso_duration_sec,
)
from src.schemas import AnnotatedTranscript, SceneBlueprint, SceneReferenceItem
from src.speaker_guard import detect_faces_ultraface
from src.style_analyzer import STYLE_DNA_PATH, analyze_video_style

_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "into",
        "out",
        "was",
        "were",
        "are",
        "his",
        "her",
        "him",
        "she",
        "you",
        "your",
        "all",
        "back",
        "just",
        "like",
        "what",
        "when",
        "how",
        "get",
        "got",
        "its",
        "not",
        "but",
        "see",
        "way",
        "guys",
        "alright",
        "shorts",
        "short",
        "clip",
        "clips",
        "gameplay",
    }
)


DEFAULT_MIN_VIEWS_YOUTUBE = 5000
DEFAULT_MIN_VPH_YOUTUBE = 50.0
DEFAULT_MIN_VIEWS_CLIPS = 1000
_CLIP_PLATFORMS = frozenset({"kick_clips", "kick", "twitch_clips", "twitch"})


def meets_viral_traction_floor(
    platform: str,
    views: int,
    views_per_hour: float,
    *,
    min_views_yt: int = DEFAULT_MIN_VIEWS_YOUTUBE,
    min_vph_yt: float = DEFAULT_MIN_VPH_YOUTUBE,
    min_views_clips: int = DEFAULT_MIN_VIEWS_CLIPS,
) -> bool:
    """Enforce minimum viral traction floor for Level 1 references (rejects small amateur uploads)."""
    plat = (platform or "youtube_shorts").strip().lower()
    if plat in _CLIP_PLATFORMS:
        return int(views) >= int(min_views_clips)
    return int(views) >= int(min_views_yt) or float(views_per_hour) >= float(min_vph_yt)


def is_blueprint_contaminated(
    blueprint: SceneBlueprint,
    *,
    min_views_yt: int = DEFAULT_MIN_VIEWS_YOUTUBE,
    min_vph_yt: float = DEFAULT_MIN_VPH_YOUTUBE,
    min_views_clips: int = DEFAULT_MIN_VIEWS_CLIPS,
) -> bool:
    """Return True if a Level 1 SceneBlueprint has <2 references or any reference below the viral floor."""
    if blueprint.match_level != "exact_scene_match":
        return False
    if len(blueprint.references) < 2:
        return True
    return any(
        not meets_viral_traction_floor(
            r.platform,
            r.views,
            r.views_per_hour,
            min_views_yt=min_views_yt,
            min_vph_yt=min_vph_yt,
            min_views_clips=min_views_clips,
        )
        for r in blueprint.references
    )


def purge_contaminated_scene_blueprints(
    db_path: Path = DEFAULT_DB_PATH,
    *,
    min_views_yt: int = DEFAULT_MIN_VIEWS_YOUTUBE,
    min_vph_yt: float = DEFAULT_MIN_VPH_YOUTUBE,
    min_views_clips: int = DEFAULT_MIN_VIEWS_CLIPS,
) -> list[str]:
    """Invalidate and delete cached `scene_blueprints` entries containing sub-threshold references."""
    init_db(db_path)
    removed: list[str] = []
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT scene_key, blueprint_json FROM scene_blueprints").fetchall()
        for row in rows:
            skey = str(row["scene_key"])
            try:
                bp = SceneBlueprint.model_validate_json(str(row["blueprint_json"]))
                contaminated = is_blueprint_contaminated(
                    bp,
                    min_views_yt=min_views_yt,
                    min_vph_yt=min_vph_yt,
                    min_views_clips=min_views_clips,
                )
            except Exception:
                contaminated = True
            if contaminated:
                conn.execute("DELETE FROM scene_blueprints WHERE scene_key = ?", (skey,))
                removed.append(skey)
    return removed


class _SceneRefMultimodalAudit(BaseModel):
    """Structured multimodal breakdown of a viral scene reference via Gemini 3.8 Flash."""

    model_config = ConfigDict(extra="forbid")

    visual_composition: str = Field(max_length=180)
    transitions_and_effects: str = Field(max_length=180)
    caption_style: str = Field(max_length=140)
    comedy_tension_rhythm: str = Field(max_length=180)
    title_cover_technique: str = Field(max_length=160)
    intro_0_3s: str = Field(max_length=160)
    outro_structure: str = Field(max_length=160)


def make_scene_key(game: str, scene_query: str) -> str:
    """Build deterministic cache key for `(game, scene_query)` in SQLite `scene_blueprints`."""
    norm_game = re.sub(r"[^a-z0-9]+", "_", game.strip().lower()).strip("_") or "generic"
    norm_q = " ".join(re.findall(r"[a-z0-9]+", scene_query.strip().lower()))
    digest = hashlib.sha256(f"{norm_game}|{norm_q}".encode()).hexdigest()[:12]
    slug = "_".join(norm_q.split()[:5])[:36] or "scene"
    return f"{norm_game}:{slug}:{digest}"


def _extract_keywords(text: str, limit: int = 8) -> list[str]:
    toks = re.findall(r"[A-Za-z0-9']+", text.lower())
    out: list[str] = []
    seen: set[str] = set()
    for t in toks:
        clean = t.strip("'")
        if len(clean) < 3 or clean in _STOPWORDS or clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
        if len(out) >= limit:
            break
    return out


def build_scene_queries(
    scene_query: str,
    transcript: AnnotatedTranscript | None = None,
    game: str = "GTA VI",
    lore_entity: str | None = None,
    extra_keywords: list[str] | None = None,
) -> list[str]:
    """Construct 1-2 targeted short-form search queries from scene metadata, lore, and transcript."""
    base_game = game.strip()
    parts: list[str] = []
    if base_game and base_game.lower() not in scene_query.lower():
        parts.append(base_game)
    if scene_query.strip():
        parts.append(scene_query.strip())
    elif transcript is not None and transcript.full_text.strip():
        kws = _extract_keywords(transcript.full_text, limit=6)
        if lore_entity:
            parts.append(lore_entity.strip())
        if kws:
            parts.append(" ".join(kws))
    elif lore_entity:
        parts.append(lore_entity.strip())

    if extra_keywords:
        parts.extend(k.strip() for k in extra_keywords if k.strip())

    primary = " ".join(parts).strip()
    if not primary:
        primary = f"{base_game or 'gaming'} funny moment"
    queries = [f"{primary} #shorts"]

    # Add a tighter 4-5 keyword fallback query for broader YouTube Shorts recall
    kws = _extract_keywords(primary, limit=5)
    if len(kws) >= 3:
        compact = f"{base_game} {' '.join(k for k in kws if k not in base_game.lower().split())[:48]} shorts".strip()
        if compact.lower() != queries[0].lower():
            queries.append(compact)
    return queries[:2]


def _keyword_relevance(title: str, query_tokens: set[str]) -> float:
    if not query_tokens:
        return 0.5
    title_tokens = set(re.findall(r"[a-z0-9]+", title.lower()))
    overlap = len(query_tokens & title_tokens)
    return round(min(1.0, overlap / max(1.0, min(4.0, float(len(query_tokens))))), 3)


def search_youtube_shorts_for_scene(
    query: str,
    max_results: int = 8,
    max_duration_sec: float = 65.0,
) -> list[dict[str, Any]]:
    """Search real YouTube Shorts for a specific scene via Data API v3 with yt-dlp Deno fallback."""
    load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=True)
    api_key = (os.getenv("YOUTUBE_API_KEY") or "").strip()
    candidates: list[dict[str, Any]] = []

    if api_key:
        try:
            sr = curl_requests.get(
                "https://www.googleapis.com/youtube/v3/search",
                params={
                    "part": "snippet",
                    "q": query,
                    "type": "video",
                    "videoDuration": "short",
                    "order": "relevance",
                    "relevanceLanguage": "en",
                    "maxResults": min(15, max(5, max_results)),
                    "key": api_key,
                },
                timeout=15,
            )
            if sr.status_code == 200:
                items = (sr.json() or {}).get("items") or []
                vid_ids = [it["id"]["videoId"] for it in items if (it.get("id") or {}).get("videoId")]
                if vid_ids:
                    vr = curl_requests.get(
                        "https://www.googleapis.com/youtube/v3/videos",
                        params={
                            "part": "snippet,statistics,contentDetails",
                            "id": ",".join(vid_ids),
                            "key": api_key,
                        },
                        timeout=15,
                    )
                    if vr.status_code == 200:
                        now = datetime.now(UTC)
                        for v in (vr.json() or {}).get("items") or []:
                            snip = v.get("snippet") or {}
                            stats = v.get("statistics") or {}
                            cdet = v.get("contentDetails") or {}
                            dur = _parse_iso_duration_sec(str(cdet.get("duration") or ""))
                            if dur <= 0.0 or dur > max_duration_sec:
                                continue
                            views = int(stats.get("viewCount") or 0)
                            likes = int(stats.get("likeCount") or 0)
                            comments = int(stats.get("commentCount") or 0)
                            pub = str(snip.get("publishedAt") or "")
                            age_h = 24.0
                            if pub:
                                try:
                                    dt = datetime.fromisoformat(pub)
                                    age_h = max(0.5, (now - dt).total_seconds() / 3600.0)
                                except ValueError:
                                    pass
                            vid = str(v.get("id") or "")
                            candidates.append(
                                {
                                    "platform": "youtube_shorts",
                                    "source_id": vid,
                                    "channel": str(snip.get("channelTitle") or ""),
                                    "title": str(snip.get("title") or "").strip(),
                                    "url": f"https://www.youtube.com/shorts/{vid}",
                                    "duration_sec": dur,
                                    "views": views,
                                    "likes": likes,
                                    "comments": comments,
                                    "views_per_hour": round(views / age_h, 1),
                                }
                            )
        except Exception:
            pass

    if len(candidates) >= 2:
        return candidates

    # Fallback via yt-dlp + Deno (still real YouTube search, zero API quota needed)
    try:
        ensure_media_path()
        ytdlp = resolve_binary_str("yt-dlp")
        deno_exe = find_deno_executable()
        deno_spec = f"deno:{deno_exe}" if deno_exe is not None else "deno"
        res = subprocess.run(
            [
                ytdlp,
                "--flat-playlist",
                "--dump-single-json",
                "--no-warnings",
                "--js-runtimes",
                deno_spec,
                "--remote-components",
                "ejs:github",
                "--",
                f"ytsearch{min(10, max(5, max_results))}:{query}",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=35,
        )
        if res.returncode == 0 and res.stdout.strip():
            data = json.loads(res.stdout)
            seen_ids = {c["source_id"] for c in candidates}
            now_dt = datetime.now(UTC)
            for ent in data.get("entries") or []:
                vid = str(ent.get("id") or "")
                if not vid or vid in seen_ids:
                    continue
                dur = float(ent.get("duration") or 0.0)
                if dur <= 0.0 or dur > max_duration_sec:
                    continue
                views = int(ent.get("view_count") or 0)
                vph = 0.0
                ts_val = ent.get("timestamp") or ent.get("release_timestamp")
                up_date = str(ent.get("upload_date") or "").strip()
                if ts_val:
                    age_h = max(0.5, (now_dt.timestamp() - float(ts_val)) / 3600.0)
                    vph = round(views / age_h, 1)
                elif len(up_date) == 8 and up_date.isdigit():
                    try:
                        dt_up = datetime.strptime(up_date, "%Y%m%d").replace(tzinfo=UTC)
                        age_h = max(1.0, (now_dt - dt_up).total_seconds() / 3600.0)
                        vph = round(views / age_h, 1)
                    except ValueError:
                        vph = 0.0
                candidates.append(
                    {
                        "platform": "youtube_shorts",
                        "source_id": vid,
                        "channel": str(ent.get("channel") or ent.get("uploader") or ""),
                        "title": str(ent.get("title") or "").strip(),
                        "url": f"https://www.youtube.com/shorts/{vid}",
                        "duration_sec": dur,
                        "views": views,
                        "likes": int(ent.get("like_count") or 0),
                        "comments": int(ent.get("comment_count") or 0),
                        "views_per_hour": vph,
                    }
                )
                seen_ids.add(vid)
    except Exception:
        pass

    return candidates


def search_kick_clips_for_scene(
    query: str,
    game: str = "GTA VI",
    limit: int = 4,
) -> list[dict[str, Any]]:
    """Search Kick clips via curl_cffi across seeded Kick streamers and keep scene-matching clips (<=60s)."""
    if "gta" not in game.lower():
        return []
    q_tokens = set(_extract_keywords(query, limit=8)) - {"gta", "vi", "gtavi", "gta6"}
    if not q_tokens:
        return []

    wl = load_whitelist()
    matched: list[dict[str, Any]] = []
    for sid, cfg in wl.items():
        if "kick" not in (cfg.get("platforms") or []):
            continue
        slug = str(cfg.get("kick_slug") or sid)
        try:
            clips = fetch_kick_gta6_clips(slug, time_window="month", limit=10)
        except Exception:
            continue
        for c in clips:
            dur = float(c.get("duration") or 0.0)
            if dur <= 0.0 or dur > 65.0:
                continue
            title = str(c.get("title") or "")
            rel = _keyword_relevance(title, q_tokens)
            if rel < 0.25:
                continue
            matched.append(
                {
                    "platform": "kick_clips",
                    "source_id": str(c.get("id") or ""),
                    "channel": slug,
                    "title": title,
                    "url": str(c.get("page_url") or ""),
                    "duration_sec": dur,
                    "views": int(c.get("views") or 0),
                    "likes": int(c.get("likes") or 0),
                    "comments": 0,
                    "views_per_hour": float(c.get("views_per_hour") or 0.0),
                }
            )
    matched.sort(key=lambda r: float(r.get("views_per_hour") or 0.0), reverse=True)
    return matched[:limit]


def _analyze_camera_framing_ultraface(video_path: Path, dur: float) -> tuple[str, float]:
    """Sample 3 frames and measure webcam/face presence, position, and area ratio using UltraFace ONNX."""
    sample_ts = [max(0.2, dur * 0.18), max(0.5, dur * 0.50), max(0.8, dur * 0.80)]
    positions: list[str] = []
    ratios: list[float] = []
    with tempfile.TemporaryDirectory(prefix="cs_scface_") as tmp:
        for idx, ts in enumerate(sample_ts):
            fp = Path(tmp) / f"f{idx}.jpg"
            res = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{ts:.2f}",
                    "-i",
                    str(video_path),
                    "-frames:v",
                    "1",
                    str(fp),
                ],
                capture_output=True,
                check=False,
                timeout=15,
            )
            if res.returncode != 0 or not fp.is_file():
                continue
            with Image.open(fp) as _im:
                img = _im.convert("RGB")
            arr = np.asarray(img, dtype=np.uint8)
            try:
                faces = detect_faces_ultraface(arr, confidence=0.52)
            except Exception:
                faces = []
            if not faces:
                continue
            best = max(faces, key=lambda b: b[4] * (b[2] - b[0]) * (b[3] - b[1]))
            x1, y1, x2, y2, _ = best
            cx = (x1 + x2) * 0.5
            cy = (y1 + y2) * 0.5
            area = max(0.0, (x2 - x1) * (y2 - y1))
            # Estimate full webcam card area (~2.8x face bbox, capped at 0.55 for split-screen)
            ratios.append(min(1.0, round(area * 2.8, 3)))
            if area >= 0.14:
                positions.append("full_face")
            elif cy < 0.40:
                positions.append("top_split")
            elif cy > 0.65 and (cx < 0.35 or cx > 0.65):
                positions.append("corner_pip")
            elif cy > 0.65:
                positions.append("bottom_split")
            else:
                positions.append("center_overlay")
    if not positions:
        return "gameplay_only_or_hidden", 0.0
    pos = max(set(positions), key=positions.count)
    return pos, round(statistics.mean(ratios), 3)


def _extract_4frame_contact_strip(video_path: Path, dur: float, out_png: Path) -> Path | None:
    ts_list = [
        min(0.25, max(0.0, dur * 0.05)),
        min(1.5, max(0.3, dur * 0.16)),
        min(3.0, max(0.7, dur * 0.32)),
        max(1.0, dur - min(2.0, dur * 0.18)),
    ]
    tw, th = 270, 480
    sheet = Image.new("RGB", (tw * 2, th * 2), (10, 12, 18))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    with tempfile.TemporaryDirectory(prefix="cs_scstrip_") as tmp:
        ok = False
        for idx, ts in enumerate(ts_list):
            fp = Path(tmp) / f"s{idx}.jpg"
            res = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{ts:.2f}",
                    "-i",
                    str(video_path),
                    "-frames:v",
                    "1",
                    "-vf",
                    f"scale={tw}:{th}:force_original_aspect_ratio=decrease,pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2",
                    str(fp),
                ],
                capture_output=True,
                check=False,
                timeout=15,
            )
            if res.returncode == 0 and fp.is_file():
                with Image.open(fp) as _im:
                    tile = _im.convert("RGB")
                gx, gy = (idx % 2) * tw, (idx // 2) * th
                sheet.paste(tile, (gx, gy))
                draw.rectangle([gx + 6, gy + 6, gx + 82, gy + 24], fill=(0, 0, 0))
                draw.text((gx + 10, gy + 9), f"t={ts:.1f}s", fill=(255, 224, 64), font=font)
                ok = True
        if not ok:
            return None
        out_png.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(out_png, format="PNG")
    return out_png


def deconstruct_reference_video(
    cand: dict[str, Any],
    *,
    outlier_ratio: float = 1.0,
    use_multimodal: bool = True,
    max_sec: int = 60,
) -> SceneReferenceItem | None:
    """Download temp MP4 (<=720p), run PySceneDetect + FFmpeg + UltraFace + Gemini 3.8 Flash, then PURGE temp MP4."""
    url = str(cand.get("url") or "").strip()
    if not url:
        return None

    with tempfile.TemporaryDirectory(prefix="cs_scref_") as tmp:
        tmp_dir = Path(tmp)
        ensure_media_path()
        ytdlp = resolve_binary_str("yt-dlp")
        out_tmpl = str(tmp_dir / "ref.%(ext)s")
        end_hms = format_hms(max(12, min(180, int(max_sec))))
        cmd = [
            ytdlp,
            "--no-playlist",
            "--no-warnings",
            "-f",
            "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best",
            "--merge-output-format",
            "mp4",
            "--download-sections",
            f"*00:00:00-{end_hms}",
            "-o",
            out_tmpl,
        ]
        if "youtube.com" in url or "youtu.be" in url:
            deno_exe = find_deno_executable()
            deno_spec = f"deno:{deno_exe}" if deno_exe is not None else "deno"
            cmd.extend(["--js-runtimes", deno_spec, "--remote-components", "ejs:github"])
        elif "kick.com" in url:
            cmd.extend(["--impersonate", "chrome"])
        cmd.extend(["--", url])

        dl_res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=90)
        mp4s = list(tmp_dir.glob("ref*.mp4"))
        if dl_res.returncode != 0 or not mp4s:
            return None
        vid_path = mp4s[0]

        # Deterministic analysis: FFmpeg + PySceneDetect + UltraFace
        dna = analyze_video_style(vid_path, label=str(cand.get("title") or url))
        dur = float(dna.get("duration_sec") or cand.get("duration_sec") or 20.0)
        psd_cuts = detect_scene_cuts_pyscenedetect(vid_path, start_trim=0.0, clip_dur=dur, threshold=27.0)
        if psd_cuts:
            cuts_count = len(psd_cuts)
            cuts_per_min = round((cuts_count / max(0.5, dur)) * 60.0, 2)
            asl = round(dur / max(1, cuts_count + 1), 2)
            first_cut = round(psd_cuts[0], 2)
        else:
            cuts_count = int(dna.get("scene_cuts_count") or 0)
            cuts_per_min = float(dna.get("cuts_per_min") or 0.0)
            asl = float(dna.get("avg_shot_len_sec") or dur)
            first_cut = float(dna.get("first_cut_sec") or dur)

        cam_pos, cam_ratio = _analyze_camera_framing_ultraface(vid_path, dur)
        wpm = float(dna.get("estimated_wpm") or 150.0)
        open_lufs = float(dna.get("opening_3s_lufs") or -20.0)

        mm_data: dict[str, str] = {}
        if use_multimodal:
            strip = _extract_4frame_contact_strip(vid_path, dur, tmp_dir / "strip.png")
            if strip is not None:
                untrusted_ref = wrap_untrusted_source_data(
                    "external_scene_reference",
                    f"platform={cand.get('platform')}\nchannel={cand.get('channel')}\ntitle={cand.get('title')}",
                )
                prompt = (
                    "Deconstruct this viral short-form reference.\n"
                    f"{untrusted_ref}\n"
                    f"Metrics: views={cand.get('views')}, likes={cand.get('likes')}, comments={cand.get('comments')}, "
                    f"vph={cand.get('views_per_hour')}, outlier_ratio={outlier_ratio:.2f}x.\n"
                    f"Deterministic telemetry: duration={dur:.1f}s, cuts={cuts_count} ({cuts_per_min}/min), "
                    f"ASL={asl:.2f}s, first_cut={first_cut:.2f}s, wpm={wpm:.0f}, opening_3s_lufs={open_lufs:.1f}, "
                    f"ultraface_camera={cam_pos} (area_ratio={cam_ratio:.2f}).\n"
                    "Inspect the 4-frame strip (0.2s, 1.5s, 3.0s, outro) and return _SceneRefMultimodalAudit JSON."
                )
                sys_inst = build_cached_system_instruction(
                    "You are Senior Viral Intelligence Analyst. Return concise empirical _SceneRefMultimodalAudit JSON."
                )
                try:
                    mm_obj, _ = generate_structured(
                        prompt,
                        _SceneRefMultimodalAudit,
                        system_instruction=sys_inst,
                        images=[strip],
                        media_resolution="MEDIA_RESOLUTION_MEDIUM",
                    )
                    mm_data = mm_obj.model_dump()
                except Exception:
                    mm_data = {}

        # Explicit unlink before TemporaryDirectory exits (guarantees 0 temp MP4 files left behind)
        for p in tmp_dir.glob("*.mp4"):
            p.unlink(missing_ok=True)

    return SceneReferenceItem(
        platform=str(cand.get("platform") or "youtube_shorts"),
        source_id=str(cand.get("source_id") or ""),
        channel=str(cand.get("channel") or ""),
        title=str(cand.get("title") or ""),
        url=url,
        duration_sec=round(dur, 2),
        views=int(cand.get("views") or 0),
        likes=int(cand.get("likes") or 0),
        comments=int(cand.get("comments") or 0),
        views_per_hour=round(float(cand.get("views_per_hour") or 0.0), 1),
        outlier_ratio=round(float(outlier_ratio), 2),
        cuts_count=cuts_count,
        cuts_per_min=cuts_per_min,
        avg_shot_len_sec=asl,
        first_cut_sec=first_cut,
        wpm=wpm,
        opening_3s_lufs=open_lufs,
        camera_position=cam_pos,
        camera_area_ratio=cam_ratio,
        intro_0_3s=mm_data.get("intro_0_3s", f"Opens at {open_lufs:.1f} LUFS with first cut at {first_cut:.1f}s"),
        outro_structure=mm_data.get("outro_structure", "Hard post-climax cut for seamless loop"),
        visual_composition=mm_data.get("visual_composition", f"{cam_pos} (cam_ratio={cam_ratio:.2f})"),
        transitions_and_effects=mm_data.get("transitions_and_effects", f"{cuts_per_min:.1f} cuts/min, motivated punch-ins"),
        caption_style=mm_data.get("caption_style", "Bold uppercase center-safe captions"),
        comedy_tension_rhythm=mm_data.get("comedy_tension_rhythm", f"ASL {asl:.2f}s setup -> payoff"),
        title_cover_technique=mm_data.get("title_cover_technique", str(cand.get("title") or "")[:90]),
    )


def format_blueprint_for_prompt(blueprint: SceneBlueprint, max_tokens: int = 300) -> str:
    """Serialize a SceneBlueprint into an actionable editing brief strictly bounded to `<= max_tokens`."""
    lo, hi = blueprint.target_duration_range_sec
    ref_lines: list[str] = []
    for idx, r in enumerate(blueprint.references[:3], start=1):
        ref_lines.append(
            f"  #{idx} [{r.platform}] {r.title[:54]} | {r.views:,}v ({r.views_per_hour:.0f} vph, {r.outlier_ratio:.1f}x) "
            f"| {r.duration_sec:.0f}s, {r.cuts_per_min:.1f} cuts/m, cam={r.camera_position}"
        )
    lines = [
        f"Match: {blueprint.match_level} (game={blueprint.game}, refs={len(blueprint.references)}, cache={blueprint.cache_hit})",
        f"Target duration: {lo:.0f}-{hi:.0f}s | Cuts/min: {blueprint.target_cuts_per_min:.1f} | ASL: {blueprint.target_avg_shot_len_sec:.2f}s | First cut: {blueprint.recommended_first_cut_sec:.1f}s",
        f"Camera: {blueprint.camera_placement_guide}",
        f"Effects/Overlay: {blueprint.overlay_and_effects_guide}",
        f"Hook 0-3s: {blueprint.hook_0_3s_formula}",
        f"Outro: {blueprint.closing_outro_formula}",
    ]
    if ref_lines:
        lines.append("Top viral references:")
        lines.extend(ref_lines)

    text = "\n".join(lines)
    # Approximate token cap (1 token ~= 4 chars)
    max_chars = max(200, max_tokens * 4)
    if len(text) > max_chars:
        text = text[: max_chars - 3].rstrip() + "..."
    return text


def _build_archetype_fallback_blueprint(
    scene_key: str,
    game: str,
    scene_query: str,
    db_path: Path = DEFAULT_DB_PATH,
) -> SceneBlueprint:
    """Level 2 fallback when <2 exact viral matches exist: synthesize from viral_style_dna.json + local RAG."""
    med_dur = 30.0
    med_cpm = 15.0
    med_asl = 3.5
    med_first = 3.0
    if STYLE_DNA_PATH.is_file():
        try:
            dna = json.loads(STYLE_DNA_PATH.read_text(encoding="utf-8"))
            meds = dna.get("empirical_medians") or {}
            med_dur = float(meds.get("duration_sec") or med_dur)
            med_cpm = float(meds.get("cuts_per_min") or med_cpm)
            med_asl = float(meds.get("avg_shot_len_sec") or med_asl)
            med_first = float(meds.get("first_cut_sec") or med_first)
        except Exception:
            pass

    rag_note = ""
    try:
        hits = query_rag(
            f"{game} {scene_query} hook pacing framing retention",
            domain="shortform_craft",
            top_k=2,
            use_vectors=False,
            db_path=db_path,
        )
        if hits:
            rag_note = "; ".join(str(h.get("heading") or "") for h in hits if h.get("heading"))
    except Exception:
        rag_note = ""

    lo = max(18.0, round(med_dur * 0.72, 1))
    hi = min(46.0, round(med_dur * 1.15, 1))
    bp = SceneBlueprint(
        scene_key=scene_key,
        game=game,
        scene_query=scene_query,
        match_level="archetype_fallback",
        cache_hit=False,
        references=[],
        target_duration_range_sec=(lo, hi),
        target_cuts_per_min=round(med_cpm, 1),
        target_avg_shot_len_sec=round(med_asl, 2),
        recommended_first_cut_sec=round(min(3.5, max(1.5, med_first)), 2),
        camera_placement_guide="split_webcam_top (ambient studio backdrop, prominent facecam <=1.8x, centered gameplay)",
        overlay_and_effects_guide=(
            "animated alpha motion_overlay (Montserrat Black, zero freeze) + 1.12x cut-guarded punch-in + synced whoosh SFX"
            + (f" | RAG: {rag_note}" if rag_note else "")
        ),
        caption_and_rhythm_guide="discrete 2-3 word Montserrat Black captions, Synchresis 0.25s margin, dead tail <=0.4s",
        hook_0_3s_formula="cold-start mid-action/dialogue with zero preamble and immediate visual curiosity gap",
        closing_outro_formula="cut cleanly right after climax shock reaction with 30ms audio out-fade for seamless loop",
        created_at=datetime.now(UTC).isoformat(),
    )
    bp.summary_prompt = format_blueprint_for_prompt(bp, max_tokens=300)
    return bp


def _synthesize_blueprint_from_refs(
    scene_key: str,
    game: str,
    scene_query: str,
    refs: list[SceneReferenceItem],
) -> SceneBlueprint:
    durs = [r.duration_sec for r in refs if r.duration_sec > 0]
    cpms = [r.cuts_per_min for r in refs]
    asls = [r.avg_shot_len_sec for r in refs if r.avg_shot_len_sec > 0]
    fcuts = [r.first_cut_sec for r in refs if r.first_cut_sec > 0]
    med_dur = statistics.median(durs) if durs else 28.0
    med_cpm = statistics.median(cpms) if cpms else 15.0
    med_asl = statistics.median(asls) if asls else 3.5
    med_first = statistics.median(fcuts) if fcuts else 2.8

    cam_positions = [r.camera_position for r in refs if r.camera_position != "unknown"]
    dom_cam = max(set(cam_positions), key=cam_positions.count) if cam_positions else "top_split"
    top_ref = refs[0]
    fx_notes = "; ".join(r.transitions_and_effects for r in refs[:2] if r.transitions_and_effects)[:190]
    hook_notes = "; ".join(r.intro_0_3s for r in refs[:2] if r.intro_0_3s)[:190]
    outro_notes = "; ".join(r.outro_structure for r in refs[:2] if r.outro_structure)[:170]

    lo = max(16.0, round(med_dur * 0.78, 1))
    hi = min(52.0, max(lo + 6.0, round(med_dur * 1.22, 1)))
    bp = SceneBlueprint(
        scene_key=scene_key,
        game=game,
        scene_query=scene_query,
        match_level="exact_scene_match",
        cache_hit=False,
        references=refs,
        target_duration_range_sec=(lo, hi),
        target_cuts_per_min=round(med_cpm, 1),
        target_avg_shot_len_sec=round(med_asl, 2),
        recommended_first_cut_sec=round(min(4.0, max(1.0, med_first)), 2),
        camera_placement_guide=f"Dominant outlier framing={dom_cam}; preserve prominent facecam + centered gameplay ({top_ref.visual_composition[:90]})",
        overlay_and_effects_guide=fx_notes or "animated motion_overlay + cut-guarded 1.12x punch-in zoom",
        caption_and_rhythm_guide=top_ref.caption_style or "bold uppercase 2-3 word captions inside Universal Safe Box",
        hook_0_3s_formula=hook_notes or "open directly on scene action/hook",
        closing_outro_formula=outro_notes or "hard cut immediately after payoff reaction",
        created_at=datetime.now(UTC).isoformat(),
    )
    bp.summary_prompt = format_blueprint_for_prompt(bp, max_tokens=300)
    return bp


def research_scene(
    scene_query: str,
    *,
    transcript: AnnotatedTranscript | None = None,
    game: str = "GTA VI",
    lore_entity: str | None = None,
    extra_keywords: list[str] | None = None,
    max_refs: int = 3,
    force_refresh: bool = False,
    use_multimodal: bool = True,
    db_path: Path = DEFAULT_DB_PATH,
    min_views_yt: int = DEFAULT_MIN_VIEWS_YOUTUBE,
    min_vph_yt: float = DEFAULT_MIN_VPH_YOUTUBE,
    min_views_clips: int = DEFAULT_MIN_VIEWS_CLIPS,
    search_yt_fn: Any = None,
    search_kick_fn: Any = None,
    deconstruct_fn: Any = None,
) -> SceneBlueprint:
    """Run 2-Level Scene-Match Intelligence with viral traction floor, SQLite cache (`scene_blueprints`) and temp MP4 purge."""
    queries = build_scene_queries(
        scene_query=scene_query,
        transcript=transcript,
        game=game,
        lore_entity=lore_entity,
        extra_keywords=extra_keywords,
    )
    canonical_query = scene_query.strip() or queries[0]
    scene_key = make_scene_key(game, canonical_query)

    # Cache hit check (auto-invalidate any stale Level 1 blueprint contaminated by sub-threshold refs)
    if not force_refresh:
        cached = get_scene_blueprint(scene_key, db_path=db_path)
        if cached is not None:
            bp = SceneBlueprint.model_validate_json(str(cached["blueprint_json"]))
            if is_blueprint_contaminated(
                bp,
                min_views_yt=min_views_yt,
                min_vph_yt=min_vph_yt,
                min_views_clips=min_views_clips,
            ):
                with _connect(db_path) as conn:
                    conn.execute("DELETE FROM scene_blueprints WHERE scene_key = ?", (scene_key,))
            else:
                bp.cache_hit = True
                return bp

    yt_search = search_yt_fn or search_youtube_shorts_for_scene
    kick_search = search_kick_fn or search_kick_clips_for_scene
    deconstruct = deconstruct_fn or deconstruct_reference_video

    raw_pool: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for q in queries:
        for item in yt_search(q, max_results=8):
            u = str(item.get("url") or "")
            if u and u not in seen_urls:
                seen_urls.add(u)
                raw_pool.append(item)
        if len(raw_pool) >= 6:
            break

    for item in kick_search(canonical_query, game=game, limit=4):
        u = str(item.get("url") or "")
        if u and u not in seen_urls:
            seen_urls.add(u)
            raw_pool.append(item)

    # Filter strictly <= 65s short-form, enforce viral traction floor, and require scene-specific keyword relevance
    game_tokens = set(re.findall(r"[a-z0-9]+", game.lower())) | {"gta", "vi", "gta6", "gtavi"}
    scene_only_tokens = set(_extract_keywords(canonical_query, limit=10)) - game_tokens
    q_tokens = scene_only_tokens or set(_extract_keywords(f"{game} {canonical_query}", limit=10))
    valid_pool = [
        c
        for c in raw_pool
        if 0.0 < float(c.get("duration_sec") or 0.0) <= 65.0
        and meets_viral_traction_floor(
            str(c.get("platform") or "youtube_shorts"),
            int(c.get("views") or 0),
            float(c.get("views_per_hour") or 0.0),
            min_views_yt=min_views_yt,
            min_vph_yt=min_vph_yt,
            min_views_clips=min_views_clips,
        )
    ]
    vphs = [max(1.0, float(c.get("views_per_hour") or 1.0)) for c in valid_pool]
    med_vph = statistics.median(vphs) if vphs else 1.0

    scored: list[tuple[float, float, dict[str, Any]]] = []
    for c in valid_pool:
        vph = max(1.0, float(c.get("views_per_hour") or 1.0))
        outlier_ratio = round(vph / max(1.0, med_vph), 2)
        title_str = str(c.get("title") or "")
        title_tokens = set(re.findall(r"[a-z0-9]+", title_str.lower()))
        rel = _keyword_relevance(title_str, q_tokens)
        matched_scene_kw = len(scene_only_tokens & title_tokens) if scene_only_tokens else 1
        min_kw = 2 if len(scene_only_tokens) >= 4 else 1
        if scene_only_tokens and (rel < 0.25 or matched_scene_kw < min_kw):
            continue
        score = rel * 0.55 + min(1.0, math.log10(vph + 1.0) / 5.0) * 0.45
        scored.append((score, outlier_ratio, c))

    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    deconstructed: list[SceneReferenceItem] = []
    if len(scored) >= 2:
        top_candidates = scored[: max(2, min(5, max_refs + 2))]
        for _, outlier_ratio, cand in top_candidates:
            if len(deconstructed) >= max_refs:
                break
            item = deconstruct(cand, outlier_ratio=outlier_ratio, use_multimodal=use_multimodal)
            if item is not None and meets_viral_traction_floor(
                item.platform,
                item.views,
                item.views_per_hour,
                min_views_yt=min_views_yt,
                min_vph_yt=min_vph_yt,
                min_views_clips=min_views_clips,
            ):
                deconstructed.append(item)

    if len(deconstructed) >= 2:
        blueprint = _synthesize_blueprint_from_refs(scene_key, game, canonical_query, deconstructed)
    else:
        blueprint = _build_archetype_fallback_blueprint(scene_key, game, canonical_query, db_path=db_path)

    upsert_scene_blueprint(
        scene_key=blueprint.scene_key,
        game=blueprint.game,
        scene_query=blueprint.scene_query,
        match_level=blueprint.match_level,
        references_count=len(blueprint.references),
        blueprint_json=blueprint.model_dump_json(),
        summary_prompt=blueprint.summary_prompt,
        db_path=db_path,
    )
    insert_telemetry(
        "scene_researcher",
        "scene_blueprint_created",
        {
            "scene_key": blueprint.scene_key,
            "game": blueprint.game,
            "match_level": blueprint.match_level,
            "references_count": len(blueprint.references),
        },
        db_path=db_path,
    )
    return blueprint
