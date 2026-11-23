"""Surgical VOD miner for YouTube / Twitch / Kick (Hito 1.2).

Never downloads full 40 GB VODs. Mines heatmap, Twitch chat z-scores, or
lightweight audio RMS peaks, then downloads only 75-second slices and enforces
a hard <= 4.0 GB disk quota on runs/.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import numpy as np
import yaml
from curl_cffi import requests as curl_requests
from dotenv import load_dotenv

from src.schemas import CandidateSlice, RunState

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = PROJECT_ROOT / "runs"
CHANNEL_PROFILE_PATH = PROJECT_ROOT / "config" / "channel_profile.yaml"
_TRAILER_TITLE_RE = re.compile(r"\b(trailer|reacting|reaction|tier\s*list|news|recap)\b", re.IGNORECASE)
_TWITCH_REACTION_RE = re.compile(r"\b(clip\s*it|lulw|omegalul|no\s*way|wtf|bro|lmao|holy|insane|w)\b", re.IGNORECASE)
_TWITCH_SUBGIFT_RE = re.compile(r"(gifted a tier|is gifting \d+|subscribed at tier|months in a row|raiders from)", re.IGNORECASE)


def _ensure_media_path() -> None:
    """Ensure .venv\\Scripts and winget Deno are on PATH for yt-dlp / TwitchDownloaderCLI."""
    extra_dirs: list[Path] = [PROJECT_ROOT / ".venv" / "Scripts"]
    local_app = os.environ.get("LOCALAPPDATA", "")
    if local_app:
        pkg_root = Path(local_app) / "Microsoft" / "WinGet" / "Packages"
        if pkg_root.exists():
            extra_dirs.extend(pkg_root.glob("DenoLand.Deno*"))
        links = Path(local_app) / "Microsoft" / "WinGet" / "Links"
        if links.exists():
            extra_dirs.append(links)
    user_deno = Path.home() / ".deno" / "bin"
    if user_deno.exists():
        extra_dirs.append(user_deno)

    current = os.environ.get("PATH", "")
    prefix = [str(p) for p in extra_dirs if p.exists() and str(p) not in current]
    if prefix:
        os.environ["PATH"] = os.pathsep.join(prefix) + os.pathsep + current


def _dir_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def get_runs_max_gb() -> float:
    if CHANNEL_PROFILE_PATH.exists():
        data = yaml.safe_load(CHANNEL_PROFILE_PATH.read_text(encoding="utf-8")) or {}
        return float(data.get("storage", {}).get("runs_max_gb", 4.0))
    return 4.0


def enforce_runs_quota(runs_root: Path = RUNS_DIR, max_gb: float | None = None, protect_dir: Path | None = None) -> float:
    """Enforce hard disk quota on runs/ by removing oldest finished run dirs if exceeded."""
    runs_root.mkdir(parents=True, exist_ok=True)
    limit_gb = max_gb if max_gb is not None else get_runs_max_gb()
    limit_bytes = int(limit_gb * (1024**3))
    used = _dir_size_bytes(runs_root)

    if used > limit_bytes:
        candidates = [
            d for d in runs_root.iterdir()
            if d.is_dir() and (protect_dir is None or d.resolve() != protect_dir.resolve())
        ]
        candidates.sort(key=lambda p: p.stat().st_mtime)
        target_bytes = int(limit_bytes * 0.80)
        for old_dir in candidates:
            if used <= target_bytes:
                break
            shutil.rmtree(old_dir, ignore_errors=True)
            used = _dir_size_bytes(runs_root)

    return round(used / (1024 * 1024), 2)


def detect_platform(url: str) -> Literal["youtube", "twitch", "kick", "local"]:
    p = Path(url)
    if p.exists():
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
    yt_exe = PROJECT_ROOT / ".venv" / "Scripts" / "yt-dlp.exe"
    exe = str(yt_exe) if yt_exe.exists() else "yt-dlp"
    cmd = [exe, "--js-runtimes", "deno", "--remote-components", "ejs:github"]
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
    """Return [(start, end, score)] non-overlapping windows sorted chronologically."""
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


def probe_kick_channel(slug: str) -> dict[str, object]:
    """Probe Kick v2 channel metadata cleanly via curl_cffi TLS impersonation (tolerates HTTP 403 WAF)."""
    url = f"https://kick.com/api/v2/channels/{slug.strip()}"
    try:
        resp = curl_requests.get(url, impersonate="chrome", timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            vods = data.get("previous_livestreams") or []
            return {
                "slug": slug,
                "status_code": 200,
                "ok": True,
                "reason": "OK",
                "is_live": bool(data.get("livestream")),
                "recent_vod_count": len(vods) if isinstance(vods, list) else 0,
            }
        reason = "WAF_403_CHALLENGE" if resp.status_code == 403 else f"HTTP_{resp.status_code}"
        return {"slug": slug, "status_code": resp.status_code, "ok": False, "reason": reason}
    except Exception as exc:  # noqa: BLE001
        return {"slug": slug, "status_code": 0, "ok": False, "reason": f"{type(exc).__name__}: {str(exc)[:100]}"}


def _windows_from_audio_rms(url: str, tmp_dir: Path, total_dur: float, top_k: int, platform: str) -> list[tuple[float, float, float]]:
    """Fallback when YouTube has no heatmap: download tiny low-bitrate audio, compute 5s RMS, then delete audio."""
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
        url,
    ])
    res = _run(cmd)
    wav_path = Path(f"{tmp_stem}.wav")
    try:
        if res.returncode != 0 or not wav_path.exists():
            raise RuntimeError(f"Audio probe failed: {(res.stderr or res.stdout)[:240]}")
        with wave.open(str(wav_path), "rb") as wf:
            sr = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
        samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        dur = len(samples) / float(sr) if sr else total_dur
        win = max(1, int(sr * 5.0))
        n_wins = max(1, len(samples) // win)
        scored: list[tuple[float, float]] = []
        for idx in range(n_wins):
            chunk = samples[idx * win : (idx + 1) * win]
            rms = float(np.sqrt(np.mean(chunk * chunk) + 1e-12))
            scored.append((idx * 5.0 + 2.5, rms))
        return _select_non_overlapping_peaks(scored, dur, top_k=top_k)
    finally:
        for f in tmp_dir.glob("full_probe_audio*"):
            f.unlink(missing_ok=True)


def _windows_from_twitch_chat(
    vod_id: str,
    tmp_dir: Path,
    total_dur: float,
    top_k: int,
) -> tuple[list[tuple[float, float, float]], str, float]:
    """Mine Twitch chat.json rolling z-score weighted by genuine reaction density ([-35s, +40s] lag window)."""
    chat_json = tmp_dir / f"twitch_chat_{vod_id}.json"
    cli = PROJECT_ROOT / ".venv" / "Scripts" / "TwitchDownloaderCLI.exe"
    exe = str(cli) if cli.exists() else "TwitchDownloaderCLI"
    res = _run([
        exe,
        "chatdownload",
        "--id",
        vod_id,
        "-o",
        str(chat_json),
        "--bttv=false",
        "--ffz=false",
        "--stv=false",
    ])
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


def _download_slice_and_audio(
    url: str,
    platform: str,
    start_sec: float,
    end_sec: float,
    out_video: Path,
    out_audio: Path,
) -> None:
    cmd = _yt_dlp_base_cmd()
    if platform == "kick":
        cmd.extend(["--impersonate", "chrome"])
    cmd.extend([
        "--download-sections",
        f"*{start_sec:.2f}-{end_sec:.2f}",
        "--force-keyframes-at-cuts",
        "-f",
        "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]/b",
        "--merge-output-format",
        "mp4",
        "-o",
        str(out_video),
        url,
    ])
    res = _run(cmd)
    if res.returncode != 0 or not out_video.exists():
        raise RuntimeError(f"Slice download failed ({start_sec}-{end_sec}): {(res.stderr or res.stdout)[:300]}")

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


def _validate_vod_source(title: str, total_dur: float, allow_short: bool, allow_trailer: bool) -> None:
    if not allow_trailer and title and _TRAILER_TITLE_RE.search(title):
        raise ValueError(f"VOD rejected by anti-trailer metadata filter: {title!r}")
    if not allow_short and total_dur > 0 and total_dur < 2700.0:
        raise ValueError(f"VOD rejected (<45 min gameplay minimum): duration={total_dur:.1f}s")


def mine_vod_slices(
    url: str,
    streamer_id: str = "default",
    run_id: str | None = None,
    top_k: int = 2,
    allow_short: bool = False,
    allow_trailer: bool = False,
) -> RunState:
    """Mine candidate 75s slices from a VOD without downloading the entire video."""
    load_dotenv(override=True)
    _ensure_media_path()

    rid = run_id or datetime.now(UTC).strftime("run_%Y%m%d_%H%M%S")
    run_dir = RUNS_DIR / rid
    run_dir.mkdir(parents=True, exist_ok=True)
    enforce_runs_quota(RUNS_DIR, protect_dir=run_dir)

    platform = detect_platform(url)
    title = ""
    total_dur = 0.0
    heatmap: list[dict[str, object]] = []

    if platform != "twitch":
        meta_cmd = _yt_dlp_base_cmd()
        if platform == "kick":
            meta_cmd.extend(["--impersonate", "chrome"])
        meta_cmd.extend(["--dump-single-json", "--skip-download", url])
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
            ((float(h["start_time"]) + float(h["end_time"])) / 2.0, float(h.get("value", 0.0)))
            for h in heatmap
            if "start_time" in h and "end_time" in h
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
            meta_res = _run(_yt_dlp_base_cmd() + ["--dump-single-json", "--skip-download", url])
            if meta_res.returncode == 0:
                meta = json.loads(meta_res.stdout)
                total_dur = float(meta.get("duration") or 120.0)
        s, e = _build_window(min(90.0, total_dur / 2.0), total_dur, lead_sec=35.0)
        windows = [(s, e, 1.0)]
        signal_source = "manual_window"

    candidates: list[CandidateSlice] = []
    for idx, (start_sec, end_sec, score) in enumerate(windows, start=1):
        sid = f"slice_{idx:02d}"
        vid_path = run_dir / f"{sid}.mp4"
        aud_path = run_dir / f"{sid}_16k.wav"
        _download_slice_and_audio(url, platform, start_sec, end_sec, vid_path, aud_path)
        candidates.append(
            CandidateSlice(
                slice_id=sid,
                source_url=url,
                platform=platform,
                start_sec=start_sec,
                end_sec=end_sec,
                duration_sec=round(max(0.1, end_sec - start_sec), 2),
                signal_source=signal_source,
                score=round(score, 4),
                slice_video_path=str(vid_path),
                slice_audio_path=str(aud_path),
            )
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
    parser = argparse.ArgumentParser(description="Clipping Studio Surgical VOD Miner")
    parser.add_argument("--url", help="YouTube/Twitch/Kick VOD URL")
    parser.add_argument("--streamer", default="default", help="Streamer preset ID")
    parser.add_argument("--run-id", default=None, help="Explicit run ID")
    parser.add_argument("--top-k", type=int, default=2, help="Number of 75s slices to download")
    parser.add_argument("--allow-short", action="store_true", help="Allow VODs shorter than 45 min (testing)")
    parser.add_argument("--allow-trailer", action="store_true", help="Allow trailer/reaction titles (testing)")
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
    )
    print(f"[MINER] run_id={state.run_id} slices={len(state.candidates)} disk_mb={state.disk_usage_mb}")
    for c in state.candidates:
        print(f"  - {c.slice_id}: {c.start_sec:.2f}s->{c.end_sec:.2f}s ({c.signal_source}, score={c.score}) -> {c.slice_video_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
