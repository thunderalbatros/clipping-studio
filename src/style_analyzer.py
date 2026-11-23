"""Deterministic Viral Style DNA Extractor (`/steal`) via FFmpeg scene-cut + ebur128 telemetry.

Zero LLM cost: measures empirical cuts-per-minute, average shot length, opening 3s loudness energy,
FPS, and resolution from reference viral clips (local MP4s or yt-dlp URLs), then synthesizes
`context/viral_style_dna.json` for Implicit Caching into `src/llm_router.py`.
"""

from __future__ import annotations

import json
import re
import shutil
import statistics
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, ConfigDict, Field

from src.db import insert_telemetry
from src.llm_router import generate_structured, reset_cached_prefix
from src.miner import PROJECT_ROOT, _ensure_media_path, find_deno_executable

STYLE_DNA_PATH = PROJECT_ROOT / "context" / "viral_style_dna.json"
_PTS_TIME_RE = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")
_EBUR_I_RE = re.compile(r"\bI:\s*(-?[0-9]+(?:\.[0-9]+)?)\s*LUFS")
_EBUR_TP_RE = re.compile(r"\bPeak:\s*(-?[0-9]+(?:\.[0-9]+)?)\s*dBFS")
_SILENCE_DUR_RE = re.compile(r"silence_duration:\s*([0-9]+(?:\.[0-9]+)?)")


class OutlierMultimodalDeconstruction(BaseModel):
    """Structured multimodal breakdown emitted by Gemini 3.8 Flash on a 4-frame outlier strip."""

    model_config = ConfigDict(extra="forbid")

    hook_0_3s_mechanic: str = Field(max_length=160)
    pattern_interrupt_type: str = Field(max_length=120)
    framing_layout: str = Field(max_length=80)
    caption_style: str = Field(max_length=100)
    retention_takeaway: str = Field(max_length=200)


def _probe_basic(video_path: Path) -> dict[str, Any]:
    res = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,r_frame_rate:format=duration",
            "-of",
            "json",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        return {"width": 0, "height": 0, "fps": 0.0, "duration_sec": 0.0}
    data = json.loads(res.stdout or "{}")
    st = (data.get("streams") or [{}])[0]
    fmt = data.get("format") or {}
    fps_raw = str(st.get("r_frame_rate") or "30/1")
    num, _, den = fps_raw.partition("/")
    fps = round(float(num) / max(1.0, float(den or 1.0)), 2) if num else 30.0
    return {
        "width": int(st.get("width") or 0),
        "height": int(st.get("height") or 0),
        "fps": fps,
        "duration_sec": round(float(fmt.get("duration") or 0.0), 2),
    }


def analyze_video_style(video_path: Path, scene_threshold: float = 0.32, label: str | None = None) -> dict[str, Any]:
    """Extract deterministic pacing & acoustic DNA from a single video using FFmpeg."""
    if not video_path.exists():
        raise FileNotFoundError(f"Video not found: {video_path}")

    meta = _probe_basic(video_path)
    dur = max(0.5, float(meta["duration_sec"]))

    # 1. Scene-change cut timestamps
    sc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(video_path),
            "-vf",
            f"select='gt(scene,{scene_threshold:.2f})',showinfo",
            "-an",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    cut_times = sorted({round(float(m.group(1)), 2) for m in _PTS_TIME_RE.finditer(sc.stderr or "")})
    # De-duplicate cuts within 0.35s (flash frames)
    dedup_cuts: list[float] = []
    for ct in cut_times:
        if not dedup_cuts or (ct - dedup_cuts[-1]) >= 0.35:
            dedup_cuts.append(ct)

    cuts_count = len(dedup_cuts)
    cuts_per_min = round((cuts_count / dur) * 60.0, 2)
    avg_shot_len = round(dur / max(1, cuts_count + 1), 2)
    first_cut_sec = dedup_cuts[0] if dedup_cuts else round(dur, 2)

    # 2. Opening 0..3s acoustic energy (hook intensity)
    open_dur = min(3.0, dur)
    au = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-t",
            f"{open_dur:.2f}",
            "-i",
            str(video_path),
            "-af",
            "ebur128=peak=true",
            "-vn",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    stderr_au = au.stderr or ""
    i_matches = _EBUR_I_RE.findall(stderr_au)
    tp_matches = _EBUR_TP_RE.findall(stderr_au)
    opening_lufs = float(i_matches[-1]) if i_matches else -24.0
    opening_tp = float(tp_matches[-1]) if tp_matches else -6.0

    aspect = "9:16" if meta["height"] > meta["width"] else ("16:9" if meta["width"] > meta["height"] else "1:1")

    # 3. Deterministic speech-activity / WPM estimate via FFmpeg silencedetect (zero extra ASR RAM overhead)
    sil = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(video_path),
            "-af",
            "silencedetect=noise=-32dB:d=0.35",
            "-vn",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    silence_total = sum(float(m.group(1)) for m in _SILENCE_DUR_RE.finditer(sil.stderr or ""))
    active_speech_ratio = max(0.05, min(1.0, (dur - silence_total) / max(1.0, dur)))
    # Empirical gaming/short-form cadence (~175 WPM during active vocal delivery)
    estimated_wpm = round(active_speech_ratio * 175.0, 1)

    return {
        "label": label or video_path.name,
        "path": str(video_path),
        "aspect": aspect,
        "width": meta["width"],
        "height": meta["height"],
        "fps": meta["fps"],
        "duration_sec": dur,
        "scene_cuts_count": cuts_count,
        "cuts_per_min": cuts_per_min,
        "avg_shot_len_sec": avg_shot_len,
        "first_cut_sec": first_cut_sec,
        "opening_3s_lufs": round(opening_lufs, 2),
        "opening_3s_peak_dbfs": round(opening_tp, 2),
        "active_speech_ratio": round(active_speech_ratio, 3),
        "estimated_wpm": estimated_wpm,
    }


def _extract_4frame_strip(video_path: Path, dur: float, out_png: Path) -> Path | None:
    """Extract a compact 2x2 contact strip (0.2s, 1.5s, 3.0s, climax) for cheap Gemini 3.8 Flash deconstruction."""
    ts_list = [
        min(0.2, max(0.0, dur * 0.05)),
        min(1.5, max(0.2, dur * 0.15)),
        min(3.0, max(0.5, dur * 0.30)),
        max(1.0, dur * 0.65),
    ]
    tw, th = 270, 480
    sheet = Image.new("RGB", (tw * 2, th * 2), (10, 12, 18))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    with tempfile.TemporaryDirectory(prefix="cs_out4_") as tmp:
        ok_any = False
        for idx, ts in enumerate(ts_list):
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
                    "-vf",
                    f"scale={tw}:{th}:force_original_aspect_ratio=decrease,pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2",
                    str(fp),
                ],
                capture_output=True,
                check=False,
                timeout=20,
            )
            if res.returncode == 0 and fp.is_file():
                with Image.open(fp) as _im:
                    tile = _im.convert("RGB")
                gx, gy = (idx % 2) * tw, (idx // 2) * th
                sheet.paste(tile, (gx, gy))
                draw.rectangle([gx + 6, gy + 6, gx + 76, gy + 24], fill=(0, 0, 0))
                draw.text((gx + 10, gy + 9), f"t={ts:.1f}s", fill=(255, 224, 64), font=font)
                ok_any = True
        if not ok_any:
            return None
        out_png.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(out_png, format="PNG")
    return out_png


def deconstruct_outlier_multimodal(
    video_path: Path,
    metrics: dict[str, Any],
    title: str = "",
    platform: str = "youtube_shorts",
) -> dict[str, Any] | None:
    """Deconstruct a viral outlier's 0-3s hook, pattern interrupt, framing & captions using Gemini 3.8 Flash."""
    with tempfile.TemporaryDirectory(prefix="cs_mmd_") as tmp:
        strip = _extract_4frame_strip(video_path, float(metrics.get("duration_sec") or 20.0), Path(tmp) / "strip.png")
        if strip is None:
            return None
        prompt = (
            f"Deconstruct this viral short-form gaming outlier ({platform}).\n"
            f"Title: {title or metrics.get('label')}\n"
            f"Deterministic FFmpeg metrics: duration={metrics.get('duration_sec')}s, "
            f"cuts_per_min={metrics.get('cuts_per_min')}, first_cut_sec={metrics.get('first_cut_sec')}s, "
            f"opening_3s_lufs={metrics.get('opening_3s_lufs')} LUFS, estimated_wpm={metrics.get('estimated_wpm')}, "
            f"aspect={metrics.get('aspect')} ({metrics.get('width')}x{metrics.get('height')}).\n"
            "Inspect the attached 4-frame strip (t=0.2s, 1.5s, 3.0s, climax) and return OutlierMultimodalDeconstruction JSON."
        )
        try:
            res, provider = generate_structured(
                prompt,
                OutlierMultimodalDeconstruction,
                system_instruction="You are Viral Intelligence Analyst. Return concise empirical JSON only.",
                images=[strip],
                media_resolution="MEDIA_RESOLUTION_MEDIUM",
            )
            data = res.model_dump()
            data["llm_provider"] = provider
            return data
        except Exception:
            return None


def download_and_analyze_url(
    url: str,
    max_sec: int = 45,
    *,
    title: str = "",
    platform: str = "youtube_shorts",
    run_multimodal: bool = False,
) -> dict[str, Any] | None:
    """Download a lightweight sample (<=720p, <=max_sec) of a public viral URL and extract Style DNA."""
    with tempfile.TemporaryDirectory(prefix="steal_dna_") as tmp:
        _ensure_media_path()
        ytdlp = str(PROJECT_ROOT / ".venv" / "Scripts" / "yt-dlp.exe")
        if not Path(ytdlp).is_file():
            ytdlp = shutil.which("yt-dlp") or "yt-dlp"
        out_tmpl = str(Path(tmp) / "sample.%(ext)s")
        cmd = [
            ytdlp,
            "--no-playlist",
            "--no-warnings",
            "-f",
            "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best",
            "--merge-output-format",
            "mp4",
            "--download-sections",
            f"*00:00:00-00:00:{min(59, max(10, max_sec)):02d}",
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
        res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=90)
        mp4s = list(Path(tmp).glob("sample*.mp4"))
        if res.returncode != 0 or not mp4s:
            return None
        dna = analyze_video_style(mp4s[0], label=title or url)
        dna["path"] = url
        dna["platform"] = platform
        if title:
            dna["title"] = title
        if run_multimodal:
            mm = deconstruct_outlier_multimodal(mp4s[0], dna, title=title, platform=platform)
            if mm is not None:
                dna["multimodal_deconstruction"] = mm
        return dna


def synthesize_style_dna(
    samples: list[dict[str, Any]],
    out_path: Path = STYLE_DNA_PATH,
    persist: bool = True,
) -> dict[str, Any]:
    """Aggregate individual video metrics into a compact `viral_style_dna.json` benchmark."""
    if not samples:
        payload: dict[str, Any] = {
            "generated_at": datetime.now(UTC).isoformat(),
            "sample_count": 0,
            "recommended_targets": {
                "target_duration_sec": [20.0, 35.0],
                "target_cuts_per_min": [12.0, 24.0],
                "target_avg_shot_len_sec": [2.5, 4.5],
                "opening_3s_lufs_floor": -16.5,
                "first_visual_pattern_interrupt_sec": 3.0,
            },
            "samples": [],
        }
    else:
        durs = [float(s["duration_sec"]) for s in samples]
        cpms = [float(s["cuts_per_min"]) for s in samples]
        shots = [float(s["avg_shot_len_sec"]) for s in samples]
        lufs_vals = [float(s["opening_3s_lufs"]) for s in samples]
        first_cuts = [float(s["first_cut_sec"]) for s in samples]
        wpms = [float(s.get("estimated_wpm") or 140.0) for s in samples]
        med_dur = round(statistics.median(durs), 1)
        med_cpm = round(statistics.median(cpms), 1)
        med_shot = round(statistics.median(shots), 2)
        med_lufs = round(statistics.median(lufs_vals), 2)
        med_first = round(statistics.median(first_cuts), 2)
        med_wpm = round(statistics.median(wpms), 1)
        payload = {
            "generated_at": datetime.now(UTC).isoformat(),
            "sample_count": len(samples),
            "empirical_medians": {
                "duration_sec": med_dur,
                "cuts_per_min": med_cpm,
                "avg_shot_len_sec": med_shot,
                "opening_3s_lufs": med_lufs,
                "first_cut_sec": med_first,
                "estimated_wpm": med_wpm,
            },
            "recommended_targets": {
                "target_duration_sec": [max(16.0, round(med_dur * 0.8, 1)), min(48.0, round(med_dur * 1.25, 1))],
                "target_cuts_per_min": [max(8.0, round(med_cpm * 0.75, 1)), max(16.0, round(med_cpm * 1.25, 1))],
                "target_avg_shot_len_sec": [
                    min(4.5, max(1.5, round(med_shot * 0.75, 2))),
                    max(4.5, min(6.5, round(med_shot * 1.25, 2))),
                ],
                "opening_3s_lufs_floor": max(-18.0, round(med_lufs - 1.5, 1)),
                "first_visual_pattern_interrupt_sec": min(4.0, max(1.5, med_first)),
                "target_wpm": [max(110.0, round(med_wpm * 0.85, 1)), min(210.0, round(med_wpm * 1.15, 1))],
            },
            "samples": samples[:15],
        }

    if persist:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        reset_cached_prefix()
        insert_telemetry("style_analyzer", "style_dna_updated", {"sample_count": payload["sample_count"]})
    return payload
