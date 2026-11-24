"""Surgical 1080p VOD/Clip miner for Twitch, Kick (Clips + Smart HLS Slicing), and YouTube Live VODs (Hito 2.0)."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin, urlparse

import numpy as np
import yaml
from curl_cffi import requests as curl_requests
from dotenv import load_dotenv

from src.db import (
    DEFAULT_DB_PATH,
    get_creator,
    seed_creators_from_yaml,
    upsert_creator,
    upsert_slice,
    upsert_vod,
)
from src.runtime_paths import (
    dir_size_bytes as _dir_size_bytes,
)
from src.runtime_paths import (
    ensure_media_path as _ensure_media_path,
)
from src.runtime_paths import (
    find_deno_executable,
    resolve_binary_str,
)
from src.schemas import CandidateSlice, RunState

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = PROJECT_ROOT / "runs"
CHANNEL_PROFILE_PATH = PROJECT_ROOT / "config" / "channel_profile.yaml"
STREAMERS_PATH = PROJECT_ROOT / "config" / "streamers.yaml"

_TRAILER_TITLE_RE = re.compile(r"\b(trailer|reacting|reaction|highlights|tier\s*list|news|recap)\b", re.IGNORECASE)
_TWITCH_REACTION_RE = re.compile(r"\b(clip\s*it|lulw|omegalul|no\s*way|wtf|bro|lmao|holy|insane|w)\b", re.IGNORECASE)
_TWITCH_SUBGIFT_RE = re.compile(r"(gifted a tier|is gifting \d+|subscribed at tier|months in a row|raiders from)", re.IGNORECASE)
_BYTERANGE_RE = re.compile(r"#EXT-X-BYTERANGE:(\d+)(?:@(\d+))?")
_RES_RE = re.compile(r"RESOLUTION=(\d+)x(\d+)")
_BW_RE = re.compile(r"BANDWIDTH=(\d+)")


PROTECTED_RUN_SUBDIRS = frozenset({"branding"})
PROTECTED_CLIP_STATUSES = frozenset({"READY_HITL", "NEEDS_VISUAL_FIX"})
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def validate_run_id(run_id: str, runs_root: Path = RUNS_DIR) -> Path:
    """Validate `--run-id` against conservative regex and guarantee resolved path stays strictly inside `runs/`."""
    rid = (run_id or "").strip()
    if not _RUN_ID_RE.fullmatch(rid):
        raise ValueError(
            f"Invalid run_id {run_id!r}: must match ^[A-Za-z0-9_-]{{1,64}}$ (no path traversal or special chars)."
        )
    base = runs_root.resolve()
    target = (base / rid).resolve()
    if not target.is_relative_to(base) or target == base:
        raise ValueError(f"Path traversal blocked for run_id={run_id!r}")
    return target


def measure_runs_usage_mb(runs_root: Path = RUNS_DIR) -> float:
    """Read-only measurement of disk usage under `runs/` in MB (never deletes files)."""
    if not runs_root.exists():
        return 0.0
    return round(_dir_size_bytes(runs_root) / (1024 * 1024), 2)


def get_runs_max_gb() -> float:
    from src.settings import load_channel_profile

    return float(load_channel_profile(CHANNEL_PROFILE_PATH).storage.runs_max_gb)


def _protected_run_ids_from_db(db_path: Path) -> set[str]:
    """Return run_ids that own READY_HITL or NEEDS_VISUAL_FIX clips in SQLite so quota cleanup never deletes them."""
    if not db_path.is_file():
        return set()
    import sqlite3

    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(clips)").fetchall()}
        if not cols:
            return set()
        if "status" in cols:
            rows = conn.execute(
                "SELECT DISTINCT run_id FROM clips WHERE status IN (?, ?)",
                ("READY_HITL", "NEEDS_VISUAL_FIX"),
            ).fetchall()
        else:
            rows = conn.execute("SELECT DISTINCT run_id FROM clips WHERE media_check_passed = 1").fetchall()
        return {str(r[0]) for r in rows if r and r[0]}
    except Exception:
        return set()
    finally:
        if conn is not None:
            conn.close()


def enforce_runs_quota(
    runs_root: Path = RUNS_DIR,
    max_gb: float | None = None,
    protect_dir: Path | None = None,
    db_path: Path | None = None,
) -> float:
    """Enforce hard disk quota on runs/ while strictly protecting `runs/branding/`, studio.db, and READY_HITL/NEEDS_VISUAL_FIX runs."""
    runs_root.mkdir(parents=True, exist_ok=True)
    limit_gb = max_gb if max_gb is not None else get_runs_max_gb()
    limit_bytes = int(limit_gb * (1024**3))
    used = _dir_size_bytes(runs_root)

    if used > limit_bytes:
        effective_db = db_path if db_path is not None else (runs_root / "studio.db")
        protected_run_ids = _protected_run_ids_from_db(effective_db)
        protect_resolved = protect_dir.resolve() if protect_dir is not None else None
        candidates = [
            d
            for d in runs_root.iterdir()
            if d.is_dir()
            and d.name not in PROTECTED_RUN_SUBDIRS
            and d.name not in protected_run_ids
            and (protect_resolved is None or d.resolve() != protect_resolved)
        ]
        candidates.sort(key=lambda p: p.stat().st_mtime)
        target_bytes = int(limit_bytes * 0.80)
        for old_dir in candidates:
            if used <= target_bytes:
                break
            shutil.rmtree(old_dir, ignore_errors=True)
            used = _dir_size_bytes(runs_root)

    return round(used / (1024 * 1024), 2)


def load_whitelist() -> dict[str, dict[str, Any]]:
    if not STREAMERS_PATH.exists():
        return {}
    data = yaml.safe_load(STREAMERS_PATH.read_text(encoding="utf-8")) or {}
    return dict(data.get("streamers") or {})


def auto_profile_creator(
    creator_id: str,
    *,
    source_url: str = "",
    tier: str = "bounty_target",
    language: str = "en",
    webcam_style: str = "auto_detect",
    style_notes: str = "",
    db_path: Path = DEFAULT_DB_PATH,
    persist: bool = True,
) -> dict[str, Any]:
    """Run one-time Auto-Profiler for any new bounty target or client creator and persist in `creators`."""
    key = creator_id.strip().lower()
    if persist:
        existing = get_creator(key, db_path=db_path)
        if existing is not None and existing.get("auto_profiled"):
            existing["dynamic_profile"] = existing.get("tier") != "radar_seed"
            existing["was_cached"] = True
            return existing

    inferred_plat = detect_platform(source_url) if source_url else "twitch"
    primary_plat = inferred_plat if inferred_plat in {"twitch", "kick", "youtube"} else "twitch"
    platforms = list(dict.fromkeys([primary_plat, "twitch", "kick", "youtube"]))
    notes = (
        style_notes.strip()
        or f"Auto-profiled ({tier}) from {primary_plat}; webcam auto-detect via Gemini Vision + UltraFace."
    )

    if persist:
        row = upsert_creator(
            creator_id=key,
            display_name=creator_id.strip() or key,
            tier=tier,
            primary_platform=primary_plat,
            platforms=platforms,
            language=language,
            webcam_style=webcam_style,
            style_notes=notes,
            auto_profiled=True,
            profile_extra={"hook_style": "high-energy reaction", "first_source_url": source_url},
            db_path=db_path,
        )
        row["dynamic_profile"] = row.get("tier") != "radar_seed"
        row["was_cached"] = False
        print(f"[AUTO_PROFILER] persisted 1-time creator profile '{key}' (tier={tier}, platform={primary_plat})")
        return row

    return {
        "creator_id": key,
        "display_name": creator_id.strip() or "creator",
        "tier": tier,
        "primary_platform": primary_plat,
        "platforms": platforms,
        "language": language,
        "webcam_style": webcam_style,
        "webcam_box": None,
        "style_notes": notes,
        "auto_profiled": True,
        "dynamic_profile": True,
        "was_cached": False,
        "hook_style": "high-energy reaction",
    }


def validate_streamer_whitelist(
    streamer_id: str,
    allow_unlisted: bool = True,
    strict: bool = False,
    *,
    source_url: str = "",
    tier: str = "bounty_target",
    db_path: Path = DEFAULT_DB_PATH,
    persist_new: bool = False,
) -> dict[str, Any]:
    """Resolve creator from SQLite `creators` (seeded from config/streamers.yaml) or run 1-time Auto-Profiler."""
    key = streamer_id.strip().lower()
    if persist_new:
        seed_creators_from_yaml(STREAMERS_PATH, db_path=db_path)

    if db_path.exists():
        existing = get_creator(key, db_path=db_path)
        if existing is not None:
            existing["dynamic_profile"] = existing.get("tier") != "radar_seed"
            return existing

    wl = load_whitelist()
    if key in wl:
        return wl[key]

    if strict and not allow_unlisted:
        raise ValueError(
            f"Streamer '{streamer_id}' is not in config/streamers.yaml or studio.db creators "
            f"(allowed seeds: {sorted(wl.keys())}; pass --allow-unlisted to auto-profile)."
        )

    print(f"[MINER] dynamic base profile activated for unlisted creator '{streamer_id}'")
    return auto_profile_creator(
        streamer_id,
        source_url=source_url,
        tier=tier,
        db_path=db_path,
        persist=persist_new,
    )


def detect_platform(url: str) -> Literal["youtube", "twitch", "kick", "local"]:
    if Path(url).exists():
        return "local"
    host = urlparse(url).netloc.lower()
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    if "twitch.tv" in host:
        return "twitch"
    if "kick.com" in host:
        return "kick"
    return "youtube"


def _yt_dlp_base_cmd() -> list[str]:
    _ensure_media_path()
    exe = resolve_binary_str("yt-dlp")
    deno_exe = find_deno_executable()
    deno_spec = f"deno:{deno_exe}" if deno_exe is not None else "deno"
    cmd = [exe, "--js-runtimes", deno_spec, "--remote-components", "ejs:github"]
    cookies_env = (os.getenv("YT_COOKIES_PATH") or "").strip()
    if cookies_env:
        cp = Path(cookies_env)
        if not cp.is_absolute():
            cp = PROJECT_ROOT / cp
        if cp.exists():
            cmd.extend(["--cookies", str(cp)])
    return cmd


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")


def probe_video_stream(video_path: Path) -> tuple[int, int, float, float]:
    """Return (width, height, fps, duration_sec) via ffprobe."""
    res = _run([
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,avg_frame_rate,r_frame_rate:format=duration",
        "-of",
        "json",
        str(video_path),
    ])
    if res.returncode != 0:
        return 0, 0, 0.0, 0.0
    data = json.loads(res.stdout or "{}")
    st = (data.get("streams") or [{}])[0]
    w = int(st.get("width") or 0)
    h = int(st.get("height") or 0)
    fps_str = str(st.get("avg_frame_rate") or st.get("r_frame_rate") or "30/1")
    fps = 30.0
    if "/" in fps_str:
        num_s, den_s = fps_str.split("/", 1)
        if float(den_s or 0) > 0:
            fps = round(float(num_s) / float(den_s), 2)
    else:
        fps = round(float(fps_str or 30.0), 2)
    dur = float((data.get("format") or {}).get("duration") or 0.0)
    return w, h, fps, dur


def _build_window(peak_t: float, total_dur: float, window_sec: float = 75.0, lead_sec: float = 25.0) -> tuple[float, float]:
    if total_dur <= window_sec:
        return 0.0, round(max(total_dur, 1.0), 2)
    start = max(0.0, peak_t - lead_sec)
    end = start + window_sec
    if end > total_dur:
        end = total_dur
        start = max(0.0, end - window_sec)
    return round(start, 2), round(end, 2)


def _select_non_overlapping_peaks(
    scored_times: list[tuple[float, float]],
    total_dur: float,
    top_k: int,
    min_gap_sec: float = 60.0,
    window_sec: float = 75.0,
    lead_sec: float = 25.0,
) -> list[tuple[float, float, float]]:
    ordered = sorted(scored_times, key=lambda x: x[1], reverse=True)
    picked: list[tuple[float, float, float]] = []
    for t_peak, score in ordered:
        if any(abs(t_peak - ((s + e) / 2.0)) < min_gap_sec for s, e, _ in picked):
            continue
        s, e = _build_window(t_peak, total_dur, window_sec=window_sec, lead_sec=lead_sec)
        if any(not (e <= ps or s >= pe) for ps, pe, _ in picked):
            continue
        picked.append((s, e, float(score)))
        if len(picked) >= top_k:
            break
    picked.sort(key=lambda x: x[0])
    return picked


def probe_kick_channel(slug: str) -> dict[str, Any]:
    """Probe Kick v2 channel metadata cleanly via curl_cffi TLS impersonation (tolerates HTTP 403 WAF)."""
    url = f"https://kick.com/api/v2/channels/{slug.strip()}"
    try:
        resp = curl_requests.get(url, impersonate="chrome", timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            vods = data.get("previous_livestreams") or []
            ls = data.get("livestream")
            playback_url = str(
                data.get("playback_url")
                or (ls.get("playback_url") if isinstance(ls, dict) else "")
                or ""
            ).strip()
            return {
                "id": data.get("id"),
                "slug": slug,
                "status_code": 200,
                "ok": True,
                "reason": "OK",
                "is_live": bool(ls),
                "playback_url": playback_url or None,
                "recent_vod_count": len(vods) if isinstance(vods, list) else 0,
            }
        reason = "WAF_403_CHALLENGE" if resp.status_code == 403 else f"HTTP_{resp.status_code}"
        return {"slug": slug, "status_code": resp.status_code, "ok": False, "reason": reason}
    except Exception as exc:
        return {"slug": slug, "status_code": 0, "ok": False, "reason": f"{type(exc).__name__}: {str(exc)[:100]}"}


def fetch_kick_gta6_clips(slug: str, time_window: str = "week", limit: int = 10) -> list[dict[str, Any]]:
    """Fetch Kick channel clips via curl_cffi, filter GTA VI category/title, and rank by views_per_hour."""
    url = f"https://kick.com/api/v2/channels/{slug.strip()}/clips?cursor=0&sort=view&time={time_window}"
    resp = curl_requests.get(url, impersonate="chrome", timeout=15)
    if resp.status_code != 200:
        print(f"[KICK_CLIPS] slug={slug} status={resp.status_code} (graceful skip)")
        return []
    raw_clips = (resp.json() or {}).get("clips") or []
    now = datetime.now(UTC)
    ranked: list[dict[str, Any]] = []
    for c in raw_clips:
        cat_name = str((c.get("category") or {}).get("name") or "")
        title = str(c.get("title") or "")
        if "grand theft" not in cat_name.lower() and "gta" not in cat_name.lower() and "gta" not in title.lower():
            continue
        if _TRAILER_TITLE_RE.search(title):
            continue
        views = int(c.get("views") or 0)
        dur = float(c.get("duration") or 0.0)
        created_s = str(c.get("created_at") or "")
        age_h = 12.0
        if created_s:
            try:
                dt = datetime.fromisoformat(created_s)
                age_h = max(0.5, (now - dt).total_seconds() / 3600.0)
            except ValueError:
                pass
        vph = round(views / age_h, 1)
        media_url = str(c.get("clip_url") or c.get("video_url") or "")
        if media_url:
            ranked.append({
                "id": str(c.get("id") or ""),
                "slug": slug,
                "title": title,
                "category": cat_name,
                "views": views,
                "views_per_hour": vph,
                "duration": dur,
                "created_at": created_s,
                "media_url": media_url,
                "page_url": f"https://kick.com/{slug}/clips/{c.get('id')}",
            })
    ranked.sort(key=lambda x: x["views_per_hour"], reverse=True)
    return ranked[:limit]


def fetch_kick_gta6_vods(slug: str, limit: int = 5) -> list[dict[str, Any]]:
    """Fetch Kick channel GTA VI VODs with HLS source playlists via curl_cffi."""
    url = f"https://kick.com/api/v2/channels/{slug.strip()}/videos"
    resp = curl_requests.get(url, impersonate="chrome", timeout=15)
    if resp.status_code != 200:
        return []
    raw = resp.json()
    vods = raw if isinstance(raw, list) else (raw.get("data") or [])
    out: list[dict[str, Any]] = []
    for v in vods:
        title = str(v.get("session_title") or "")
        src = str(v.get("source") or "")
        dur_sec = float(v.get("duration") or 0.0) / 1000.0
        if not src:
            continue
        if "gta" in title.lower() or "grand theft" in title.lower():
            out.append({"title": title, "duration_sec": dur_sec, "source": src, "slug": slug})
    return out[:limit]


def download_kick_hls_slice(
    m3u8_url: str,
    start_sec: float,
    end_sec: float,
    out_video: Path,
    out_audio: Path,
    allow_720p: bool = False,
) -> tuple[int, int, float, float]:
    """Smart HLS Slicer for Kick VODs/Clips using curl_cffi (supports #EXT-X-BYTERANGE, bypasses 403)."""
    r_master = curl_requests.get(m3u8_url, impersonate="chrome", timeout=20)
    if r_master.status_code != 200:
        raise RuntimeError(f"Kick HLS playlist fetch failed: HTTP {r_master.status_code}")

    playlist_url = m3u8_url
    text = r_master.text
    if "#EXT-X-STREAM-INF" in text:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        variants: list[tuple[int, int, int, str]] = []
        for idx, ln in enumerate(lines):
            if ln.startswith("#EXT-X-STREAM-INF") and idx + 1 < len(lines):
                m_res = _RES_RE.search(ln)
                m_bw = _BW_RE.search(ln)
                vw = int(m_res.group(1)) if m_res else 0
                vh = int(m_res.group(2)) if m_res else 0
                bw = int(m_bw.group(1)) if m_bw else 0
                uri = urljoin(m3u8_url, lines[idx + 1])
                variants.append((vh, bw, vw, uri))
        if not variants:
            raise RuntimeError("No HLS variants found in Kick master playlist")
        variants.sort(key=lambda x: (x[0], x[1]), reverse=True)
        best_h, _, _, playlist_url = variants[0]
        if best_h < 1080 and not allow_720p:
            raise RuntimeError(f"Kick HLS max height {best_h}p < 1080p required (pass --allow-720p to override)")
        r_media = curl_requests.get(playlist_url, impersonate="chrome", timeout=20)
        if r_media.status_code != 200:
            raise RuntimeError(f"Kick media playlist fetch failed: HTTP {r_media.status_code}")
        text = r_media.text

    segments: list[tuple[float, float, str, tuple[int, int] | None]] = []
    cur_t = 0.0
    pending_dur = 0.0
    pending_br: tuple[int, int] | None = None
    last_br_end = 0
    for raw_ln in text.splitlines():
        ln = raw_ln.strip()
        if not ln:
            continue
        if ln.startswith("#EXTINF:"):
            pending_dur = float(ln.split(":", 1)[1].split(",", 1)[0])
        elif ln.startswith("#EXT-X-BYTERANGE:"):
            m_br = _BYTERANGE_RE.match(ln)
            if m_br:
                length = int(m_br.group(1))
                offset = int(m_br.group(2)) if m_br.group(2) is not None else last_br_end
                pending_br = (offset, offset + length - 1)
                last_br_end = offset + length
        elif not ln.startswith("#"):
            seg_url = urljoin(playlist_url, ln)
            seg_end = cur_t + pending_dur
            segments.append((cur_t, seg_end, seg_url, pending_br))
            cur_t = seg_end
            pending_dur = 0.0
            pending_br = None

    selected = [s for s in segments if s[1] > start_sec and s[0] < end_sec]
    if not selected:
        selected = segments[: max(1, min(len(segments), 38))]

    with tempfile.TemporaryDirectory(prefix="cs_kick_hls_") as tmp:
        raw_ts = Path(tmp) / "slice.ts"
        with raw_ts.open("wb") as fh:
            for _, _, seg_url, br in selected:
                headers = {"Range": f"bytes={br[0]}-{br[1]}"} if br else {}
                sr = curl_requests.get(seg_url, headers=headers, impersonate="chrome", timeout=25)
                if sr.status_code not in {200, 206}:
                    raise RuntimeError(f"Kick HLS segment download failed: HTTP {sr.status_code}")
                fh.write(sr.content)

        out_video.parent.mkdir(parents=True, exist_ok=True)
        remux = _run([
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(raw_ts),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(out_video),
        ])
        if remux.returncode != 0 or not out_video.exists():
            raise RuntimeError(f"Kick HLS remux failed: {remux.stderr[:240]}")

    w, h, fps, dur = probe_video_stream(out_video)
    if h < 1080 and not allow_720p:
        out_video.unlink(missing_ok=True)
        raise RuntimeError(f"Downloaded Kick slice is {w}x{h} (< 1080p floor); pass --allow-720p to override")

    ares = _run([
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(out_video),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(out_audio),
    ])
    if ares.returncode != 0 or not out_audio.exists():
        raise RuntimeError(f"Audio extraction failed: {ares.stderr[:240]}")
    return w, h, fps, dur


def _windows_from_audio_rms(url: str, tmp_dir: Path, total_dur: float, top_k: int, platform: str) -> list[tuple[float, float, float]]:
    tmp_stem = tmp_dir / "full_probe_audio"
    cmd = _yt_dlp_base_cmd()
    if platform == "kick":
        cmd.extend(["--impersonate", "chrome"])
    cmd.extend([
        "-f",
        "ba[abr<=96]/ba",
        "-x",
        "--audio-format",
        "wav",
        "--postprocessor-args",
        "ffmpeg:-ac 1 -ar 16000",
        "-o",
        f"{tmp_stem}.%(ext)s",
        "--",
        url,
    ])
    res = _run(cmd)
    wav_path = Path(f"{tmp_stem}.wav")
    try:
        if res.returncode != 0 or not wav_path.exists():
            raise RuntimeError(f"Audio probe failed: {(res.stderr or res.stdout)[:240]}")
        scored: list[tuple[float, float]] = []
        with wave.open(str(wav_path), "rb") as wf:
            sr = wf.getframerate() or 16000
            n_channels = max(1, wf.getnchannels())
            total_frames = wf.getnframes()
            dur = total_frames / float(sr) if total_frames > 0 else total_dur
            win_frames = max(1, int(sr * 5.0))
            idx = 0
            # Stream 5-second chunks (~160 KB RAM) instead of loading multi-hour WAV into 3.5 GB float32 RAM
            while True:
                raw = wf.readframes(win_frames)
                if not raw:
                    break
                chunk = np.frombuffer(raw, dtype=np.int16)
                if chunk.size == 0:
                    break
                if n_channels > 1:
                    chunk = chunk[::n_channels]
                chunk_f = chunk.astype(np.float32) / 32768.0
                rms = float(np.sqrt(float(np.mean(chunk_f * chunk_f)) + 1e-12))
                scored.append((idx * 5.0 + 2.5, rms))
                idx += 1
                del chunk, chunk_f
        return _select_non_overlapping_peaks(scored, dur, top_k=top_k)
    finally:
        for f in tmp_dir.glob("full_probe_audio*"):
            f.unlink(missing_ok=True)


def _windows_from_twitch_chat(
    vod_id: str,
    tmp_dir: Path,
    total_dur: float,
    top_k: int,
    chat_window_sec: int = 5400,
) -> tuple[list[tuple[float, float, float]], str, float]:
    """Mine Twitch chat.json rolling z-score weighted by genuine reaction density (bounded window for mega-VODs)."""
    chat_json = tmp_dir / f"twitch_chat_{vod_id}.json"
    exe = resolve_binary_str("TwitchDownloaderCLI")

    # Probe duration cheaply so mega-VODs (>3h) scan a bounded 90-min active gameplay window in <60s
    probed_dur = total_dur
    if probed_dur <= 0 and chat_window_sec > 0:
        pr = _run(_yt_dlp_base_cmd() + ["--print", "duration", "--skip-download", "--", f"https://www.twitch.tv/videos/{vod_id}"])
        if pr.returncode == 0:
            try:
                probed_dur = float((pr.stdout or "").strip().splitlines()[-1])
            except (ValueError, IndexError):
                probed_dur = 0.0

    cmd = [
        exe,
        "chatdownload",
        "--id",
        vod_id,
        "-o",
        str(chat_json),
        "--threads",
        "6",
        "--collision",
        "Overwrite",
        "--log-level",
        "Error",
        "--bttv=false",
        "--ffz=false",
        "--stv=false",
    ]
    if chat_window_sec > 0 and probed_dur > chat_window_sec + 2400:
        begin_s = int(min(1800, probed_dur * 0.15))
        end_s = int(min(probed_dur - 120, begin_s + chat_window_sec))
        cmd.extend(["-b", f"{begin_s}s", "-e", f"{end_s}s"])

    res = _run(cmd)
    if res.returncode != 0 or not chat_json.exists():
        raise RuntimeError(f"Twitch chatdownload failed: {(res.stderr or res.stdout)[:200]}")
    try:
        data = json.loads(chat_json.read_text(encoding="utf-8"))
        vid_info = data.get("video", {}) or {}
        v_title = str(vid_info.get("title") or "")
        v_dur = float(vid_info.get("length") or total_dur or 0.0)
        comments = data.get("comments", [])
        buckets: dict[int, list[str]] = {}
        for c in comments:
            sec = int(float(c.get("content_offset_seconds", 0.0)))
            b = max(0, sec // 5)
            body = str(c.get("message", {}).get("body", "")).strip().lower()
            if body and not _TWITCH_SUBGIFT_RE.search(body):
                buckets.setdefault(b, []).append(body)
        if not buckets:
            return [], v_title, v_dur
        max_b = max(buckets) + 1
        counts = np.zeros(max_b, dtype=np.float32)
        for b, msgs in buckets.items():
            uniq_ratio = len(set(msgs)) / max(1, len(msgs))
            if uniq_ratio >= 0.35 or len(msgs) <= 4:
                react_hits = sum(1 for m in msgs if _TWITCH_REACTION_RE.search(m))
                counts[b] = float(len(msgs)) + 0.65 * float(react_hits)
        mean = float(np.mean(counts))
        std = float(np.std(counts)) + 1e-6
        scored = [(b * 5.0 + 2.5, float((counts[b] - mean) / std)) for b in range(max_b) if counts[b] > 0]
        dur_ref = v_dur or (max_b * 5.0)
        windows = _select_non_overlapping_peaks(scored, dur_ref, top_k=top_k, lead_sec=35.0)
        return windows, v_title, dur_ref
    finally:
        chat_json.unlink(missing_ok=True)


def _download_slice_and_audio_ytdlp(
    url: str,
    platform: str,
    start_sec: float,
    end_sec: float,
    out_video: Path,
    out_audio: Path,
    allow_720p: bool = False,
) -> tuple[int, int, float, float]:
    """Download surgical slice enforcing native >=1080p quality floor (1080p60/source preferred)."""
    fmt_1080 = (
        "bestvideo[height>=1080][fps>=50][ext=mp4]+bestaudio[ext=m4a]/"
        "bestvideo[height>=1080]+bestaudio/best[height>=1080]"
    )
    fmt_fallback = "bv*[height>=720][ext=mp4]+ba[ext=m4a]/b[height>=720]/b"

    def _try_fetch(fmt: str) -> subprocess.CompletedProcess[str]:
        cmd = _yt_dlp_base_cmd()
        if platform == "kick":
            cmd.extend(["--impersonate", "chrome"])
        cmd.extend([
            "--download-sections",
            f"*{start_sec:.2f}-{end_sec:.2f}",
            "--force-keyframes-at-cuts",
            "-f",
            fmt,
            "--merge-output-format",
            "mp4",
            "-o",
            str(out_video),
            "--",
            url,
        ])
        return _run(cmd)

    res = _try_fetch(fmt_1080)
    if (res.returncode != 0 or not out_video.exists()) and allow_720p:
        res = _try_fetch(fmt_fallback)
    if res.returncode != 0 or not out_video.exists():
        raise RuntimeError(f"1080p slice download failed ({start_sec}-{end_sec}): {(res.stderr or res.stdout)[:300]}")

    w, h, fps, dur = probe_video_stream(out_video)
    if h < 1080 and not allow_720p:
        out_video.unlink(missing_ok=True)
        raise RuntimeError(
            f"Rejected sub-1080p slice ({w}x{h} @ {fps}fps < 1080p strict floor; pass --allow-720p to override)."
        )

    ares = _run([
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(out_video),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(out_audio),
    ])
    if ares.returncode != 0 or not out_audio.exists():
        raise RuntimeError(f"Audio extraction failed: {ares.stderr[:240]}")
    return w, h, fps, dur


def _validate_vod_source(title: str, total_dur: float, allow_short: bool, allow_trailer: bool) -> None:
    if not allow_trailer and title and _TRAILER_TITLE_RE.search(title):
        raise ValueError(f"VOD rejected by anti-trailer/highlights metadata filter: {title!r}")
    if not allow_short and total_dur > 0 and total_dur < 2700.0:
        raise ValueError(f"VOD rejected (<45 min unedited livestream minimum): duration={total_dur:.1f}s")


def _mine_kick_source(
    url: str,
    streamer_id: str,
    run_dir: Path,
    top_k: int,
    allow_720p: bool,
) -> tuple[list[CandidateSlice], str, float]:
    """Mine Kick clips (ranked by views_per_hour) or VOD windows via Smart HLS Slicing."""
    parsed = urlparse(url)
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    slug = parts[0] if parts else streamer_id
    candidates: list[CandidateSlice] = []
    vod_title = f"Kick {slug} GTA VI"
    vod_dur = 0.0

    # Direct Kick clip URL: https://kick.com/<slug>/clips/<clip_id>
    if len(parts) >= 3 and parts[1] == "clips":
        clip_id = parts[2]
        clips = fetch_kick_gta6_clips(slug, time_window="month", limit=40)
        match = next((c for c in clips if c["id"] == clip_id), None)
        if not match:
            r_clip = curl_requests.get(f"https://kick.com/api/v2/clips/{clip_id}", impersonate="chrome", timeout=15)
            if r_clip.status_code == 200:
                cd = (r_clip.json() or {}).get("clip") or {}
                match = {
                    "id": clip_id,
                    "title": str(cd.get("title") or clip_id),
                    "views_per_hour": float(cd.get("views") or 100.0),
                    "duration": float(cd.get("duration") or 45.0),
                    "media_url": str(cd.get("clip_url") or cd.get("video_url") or ""),
                    "page_url": url,
                }
        if not match or not match.get("media_url"):
            raise RuntimeError(f"Could not resolve Kick clip media_url for {clip_id}")
        vod_title = str(match["title"])
        vod_dur = float(match["duration"])
        sid = "slice_01"
        vid_path, aud_path = run_dir / f"{sid}.mp4", run_dir / f"{sid}_16k.wav"
        w, h, fps, dur = download_kick_hls_slice(
            str(match["media_url"]), 0.0, max(75.0, vod_dur + 5.0), vid_path, aud_path, allow_720p=allow_720p
        )
        candidates.append(
            CandidateSlice(
                slice_id=sid,
                source_url=url,
                platform="kick",
                start_sec=0.0,
                end_sec=round(dur, 2),
                duration_sec=round(max(0.1, dur), 2),
                signal_source="kick_clips_rank",
                score=float(match["views_per_hour"]),
                slice_video_path=str(vid_path),
                slice_audio_path=str(aud_path),
                source_width=w,
                source_height=h,
                source_fps=fps,
            )
        )
        return candidates, vod_title, vod_dur

    # Channel URL (https://kick.com/<slug>) -> top GTA VI clips by views_per_hour, fallback to latest VOD Smart HLS
    clips = fetch_kick_gta6_clips(slug, time_window="week", limit=max(top_k * 2, 6))
    usable_clips = [c for c in clips if 22.0 <= float(c["duration"]) <= 95.0][:top_k]
    if usable_clips:
        vod_title = f"Kick {slug} Top GTA VI Clips"
        for idx, c in enumerate(usable_clips, start=1):
            sid = f"slice_{idx:02d}"
            vid_path, aud_path = run_dir / f"{sid}.mp4", run_dir / f"{sid}_16k.wav"
            w, h, fps, dur = download_kick_hls_slice(
                str(c["media_url"]), 0.0, max(75.0, float(c["duration"]) + 5.0), vid_path, aud_path, allow_720p=allow_720p
            )
            candidates.append(
                CandidateSlice(
                    slice_id=sid,
                    source_url=str(c["page_url"]),
                    platform="kick",
                    start_sec=0.0,
                    end_sec=round(dur, 2),
                    duration_sec=round(max(0.1, dur), 2),
                    signal_source="kick_clips_rank",
                    score=float(c["views_per_hour"]),
                    slice_video_path=str(vid_path),
                    slice_audio_path=str(aud_path),
                    source_width=w,
                    source_height=h,
                    source_fps=fps,
                )
            )
        return candidates, vod_title, sum(c.duration_sec for c in candidates)

    vods = fetch_kick_gta6_vods(slug, limit=3)
    if not vods:
        raise RuntimeError(f"No Kick GTA VI clips or VODs found for channel '{slug}'")
    vod = vods[0]
    vod_title = str(vod["title"])
    vod_dur = float(vod["duration_sec"])
    offsets = [round(vod_dur * frac, 2) for frac in (0.35, 0.65)][:top_k]
    for idx, st_sec in enumerate(offsets, start=1):
        sid = f"slice_{idx:02d}"
        vid_path, aud_path = run_dir / f"{sid}.mp4", run_dir / f"{sid}_16k.wav"
        w, h, fps, dur = download_kick_hls_slice(
            str(vod["source"]), st_sec, st_sec + 75.0, vid_path, aud_path, allow_720p=allow_720p
        )
        candidates.append(
            CandidateSlice(
                slice_id=sid,
                source_url=url,
                platform="kick",
                start_sec=st_sec,
                end_sec=round(st_sec + dur, 2),
                duration_sec=round(max(0.1, dur), 2),
                signal_source="kick_hls_window",
                score=1.0,
                slice_video_path=str(vid_path),
                slice_audio_path=str(aud_path),
                source_width=w,
                source_height=h,
                source_fps=fps,
            )
        )
    return candidates, vod_title, vod_dur


def mine_vod_slices(
    url: str,
    streamer_id: str = "xqc",
    run_id: str | None = None,
    top_k: int = 2,
    allow_short: bool = False,
    allow_trailer: bool = False,
    allow_720p: bool = False,
    allow_unlisted: bool = False,
) -> RunState:
    """Mine candidate 1080p slices from Twitch, Kick, or YouTube Live VODs without downloading full VODs."""
    load_dotenv(override=True)
    _ensure_media_path()
    validate_streamer_whitelist(streamer_id, allow_unlisted=allow_unlisted, source_url=url, persist_new=True)

    rid = run_id or datetime.now(UTC).strftime("run_%Y%m%d_%H%M%S")
    run_dir = validate_run_id(rid, RUNS_DIR)
    run_dir.mkdir(parents=True, exist_ok=True)
    enforce_runs_quota(RUNS_DIR, protect_dir=run_dir)

    platform = detect_platform(url)
    title = ""
    total_dur = 0.0
    candidates: list[CandidateSlice] = []

    if platform == "kick":
        candidates, title, total_dur = _mine_kick_source(url, streamer_id, run_dir, top_k=top_k, allow_720p=allow_720p)
    else:
        heatmap: list[dict[str, object]] = []
        if platform != "twitch":
            meta_cmd = _yt_dlp_base_cmd() + ["--dump-single-json", "--skip-download", "--", url]
            meta_res = _run(meta_cmd)
            if meta_res.returncode != 0:
                raise RuntimeError(f"Metadata probe failed: {(meta_res.stderr or meta_res.stdout)[:300]}")
            meta = json.loads(meta_res.stdout)
            title = str(meta.get("title") or "")
            total_dur = float(meta.get("duration") or 0.0)
            heatmap = meta.get("heatmap") or []
            _validate_vod_source(title, total_dur, allow_short=allow_short, allow_trailer=allow_trailer)

        signal_source: Literal["youtube_heatmap", "twitch_chat_zscore", "audio_rms", "manual_window"]
        windows: list[tuple[float, float, float]] = []

        if platform == "youtube" and isinstance(heatmap, list) and len(heatmap) > 0:
            scored = [
                ((float(str(h["start_time"])) + float(str(h["end_time"]))) / 2.0, float(str(h.get("value", 0.0))))
                for h in heatmap
                if isinstance(h, dict) and "start_time" in h and "end_time" in h
            ]
            windows = _select_non_overlapping_peaks(scored, total_dur, top_k=top_k)
            signal_source = "youtube_heatmap"
        elif platform == "twitch":
            m = re.search(r"/videos/(\d+)", url)
            if m:
                windows, title, total_dur = _windows_from_twitch_chat(m.group(1), run_dir, total_dur, top_k=top_k)
                _validate_vod_source(title, total_dur, allow_short=allow_short, allow_trailer=allow_trailer)
                signal_source = "twitch_chat_zscore"
            else:
                windows = _windows_from_audio_rms(url, run_dir, total_dur, top_k=top_k, platform=platform)
                signal_source = "audio_rms"
        else:
            windows = _windows_from_audio_rms(url, run_dir, total_dur, top_k=top_k, platform=platform)
            signal_source = "audio_rms"

        if not windows:
            if total_dur <= 0:
                meta_res = _run(_yt_dlp_base_cmd() + ["--dump-single-json", "--skip-download", "--", url])
                if meta_res.returncode == 0:
                    meta = json.loads(meta_res.stdout)
                    total_dur = float(meta.get("duration") or 120.0)
            s, e = _build_window(min(90.0, total_dur / 2.0), total_dur, lead_sec=35.0)
            windows = [(s, e, 1.0)]
            signal_source = "manual_window"

        for idx, (start_sec, end_sec, score) in enumerate(windows, start=1):
            sid = f"slice_{idx:02d}"
            vid_path = run_dir / f"{sid}.mp4"
            aud_path = run_dir / f"{sid}_16k.wav"
            w, h, fps, dur = _download_slice_and_audio_ytdlp(
                url, platform, start_sec, end_sec, vid_path, aud_path, allow_720p=allow_720p
            )
            candidates.append(
                CandidateSlice(
                    slice_id=sid,
                    source_url=url,
                    platform=platform,
                    start_sec=start_sec,
                    end_sec=end_sec,
                    duration_sec=round(max(0.1, dur or (end_sec - start_sec)), 2),
                    signal_source=signal_source,
                    score=round(score, 4),
                    slice_video_path=str(vid_path),
                    slice_audio_path=str(aud_path),
                    source_width=w,
                    source_height=h,
                    source_fps=fps,
                )
            )

    upsert_vod(url, platform, streamer_id, title=title, duration_sec=total_dur, run_id=rid, status="mined")
    for c in candidates:
        upsert_slice(
            run_id=rid,
            slice_id=c.slice_id,
            source_url=c.source_url,
            platform=c.platform,
            start_sec=c.start_sec,
            end_sec=c.end_sec,
            signal_source=c.signal_source,
            score=c.score,
            video_path=c.slice_video_path,
            source_width=c.source_width,
            source_height=c.source_height,
            source_fps=c.source_fps,
        )

    used_mb = enforce_runs_quota(RUNS_DIR, protect_dir=run_dir)
    state = RunState(
        run_id=rid,
        source_url=url,
        streamer_id=streamer_id,
        stage="mined",
        candidates=candidates,
        disk_usage_mb=used_mb,
        updated_at=datetime.now(UTC).isoformat(),
    )
    (run_dir / "state.json").write_text(state.model_dump_json(indent=2), encoding="utf-8")
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description="Clipping Studio 1080p Surgical Miner (Twitch / Kick / YouTube)")
    parser.add_argument("--url", help="Twitch / Kick / YouTube VOD or Clip URL")
    parser.add_argument("--streamer", default="xqc", help="Whitelist streamer ID")
    parser.add_argument("--run-id", default=None, help="Explicit run ID")
    parser.add_argument("--top-k", type=int, default=2, help="Number of slices to download")
    parser.add_argument("--allow-short", action="store_true", help="Allow VODs < 45 min")
    parser.add_argument("--allow-trailer", action="store_true", help="Allow trailer/reaction titles")
    parser.add_argument("--allow-720p", action="store_true", help="Allow sub-1080p fallback")
    parser.add_argument("--allow-unlisted", action="store_true", help="Allow streamer outside config/streamers.yaml")
    parser.add_argument("--probe-kick", default=None, help="Optional Kick channel slug to probe via curl_cffi")
    args = parser.parse_args()

    if args.probe_kick:
        info = probe_kick_channel(args.probe_kick)
        print(f"[KICK_PROBE] {json.dumps(info)}")
        if not args.url:
            return 0
    if not args.url:
        parser.error("--url is required unless only --probe-kick is specified")

    state = mine_vod_slices(
        args.url,
        streamer_id=args.streamer,
        run_id=args.run_id,
        top_k=args.top_k,
        allow_short=args.allow_short,
        allow_trailer=args.allow_trailer,
        allow_720p=args.allow_720p,
        allow_unlisted=args.allow_unlisted,
    )
    print(f"[MINER] run_id={state.run_id} slices={len(state.candidates)} disk_mb={state.disk_usage_mb}")
    for c in state.candidates:
        print(
            f"  - {c.slice_id}: {c.source_width}x{c.source_height}@{c.source_fps}fps "
            f"{c.start_sec:.2f}s->{c.end_sec:.2f}s ({c.signal_source}, score={c.score})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
