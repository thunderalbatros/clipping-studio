"""Owned-Channel YouTube Long-Form 16:9 Compiler (1920x1080, chapters, 1280x720 thumbnail, -14 LUFS)."""

from __future__ import annotations

import json
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from src.db import DEFAULT_DB_PATH, insert_telemetry, list_approved_clips
from src.miner import PROJECT_ROOT
from src.renderer import BUNDLED_FONT_PATH, probe_video_stream

SFX_WHOOSH = PROJECT_ROOT / "assets" / "sfx" / "whoosh.wav"


def _fmt_chapter_ts(sec: float) -> str:
    total = max(0, round(sec))
    mins, secs = divmod(total, 60)
    hrs, mins = divmod(mins, 60)
    return f"{hrs:02d}:{mins:02d}:{secs:02d}" if hrs else f"{mins:02d}:{secs:02d}"


def _extract_best_frame(video_path: Path, out_png: Path) -> Path:
    _, _, _, dur = probe_video_stream(video_path)
    ts = max(0.5, min(dur * 0.4, max(0.5, dur - 1.0)))
    subprocess.run(
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
            str(out_png),
        ],
        capture_output=True,
        check=True,
        timeout=30,
    )
    return out_png


def generate_youtube_thumbnail(
    source_video: Path,
    headline: str,
    out_jpg: Path,
    badge_text: str = "GTA VI",
) -> Path:
    """Generate a high-contrast 1280x720 (<2 MB) YouTube Long-Form thumbnail with Montserrat Black."""
    out_jpg.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="yt_thumb_") as tmp:
        raw_png = _extract_best_frame(source_video, Path(tmp) / "frame.png")
        img = Image.open(raw_png).convert("RGB")
        # Fit 1280x720 cleanly (if 9:16 input, center over blurred landscape canvas)
        w, h = img.size
        if h > w:
            bg = img.resize((1280, 720), Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(18))
            bg = ImageEnhance.Brightness(bg).enhance(0.45)
            fg_h = 720
            fg_w = int(w * (fg_h / h))
            fg = img.resize((fg_w, fg_h), Image.Resampling.LANCZOS)
            bg.paste(fg, ((1280 - fg_w) // 2, 0))
            canvas = bg
        else:
            canvas = img.resize((1280, 720), Image.Resampling.LANCZOS)

        canvas = ImageEnhance.Contrast(canvas).enhance(1.12)
        canvas = ImageEnhance.Color(canvas).enhance(1.10)

        # Dark bottom gradient band for legibility
        overlay = Image.new("RGBA", (1280, 720), (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)
        for y in range(440, 720):
            alpha = int(205 * ((y - 440) / 280.0))
            odraw.line([(0, y), (1280, y)], fill=(8, 10, 14, alpha))

        # Top-left badge pill
        odraw.rounded_rectangle([(42, 36), (255, 102)], radius=16, fill=(255, 214, 0, 240))
        composed = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")
        draw = ImageDraw.Draw(composed)

        font_path = str(BUNDLED_FONT_PATH) if BUNDLED_FONT_PATH.exists() else None
        badge_font = ImageFont.truetype(font_path, 38) if font_path else ImageFont.load_default()
        title_font = ImageFont.truetype(font_path, 86) if font_path else ImageFont.load_default()

        draw.text((66, 48), badge_text.upper()[:12], font=badge_font, fill=(14, 14, 18))

        clean_hl = " ".join(headline.upper().split()[:5])
        bbox = draw.textbbox((0, 0), clean_hl, font=title_font, stroke_width=6)
        tw = bbox[2] - bbox[0]
        if tw > 1180 and font_path:
            title_font = ImageFont.truetype(font_path, 66)
            bbox = draw.textbbox((0, 0), clean_hl, font=title_font, stroke_width=5)
            tw = bbox[2] - bbox[0]
        tx = max(44, (1280 - tw) // 2)
        draw.text(
            (tx, 560),
            clean_hl,
            font=title_font,
            fill=(255, 255, 255),
            stroke_width=6,
            stroke_fill=(0, 0, 0),
        )
        composed.save(out_jpg, format="JPEG", quality=92, optimize=True)
    return out_jpg


def _normalize_segment_16_9(src_mp4: Path, out_seg: Path, start_sec: float | None = None, end_sec: float | None = None) -> float:
    """Normalize a single clip (16:9 or 9:16) into 1920x1080 60fps H.264 + 48kHz stereo segment."""
    w, h, _, dur = probe_video_stream(src_mp4)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    if start_sec is not None and start_sec > 0:
        cmd.extend(["-ss", f"{start_sec:.3f}"])
    if start_sec is not None and end_sec is not None and end_sec > start_sec:
        cmd.extend(["-t", f"{end_sec - start_sec:.3f}"])
    cmd.extend(["-i", str(src_mp4)])

    if h > w:
        # 9:16 vertical clip -> crisp centered pillarbox over dark blurred 16:9 background
        vf = (
            "[0:v]split=2[bg][fg];"
            "[bg]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,"
            "boxblur=24:4,eq=brightness=-0.18:saturation=0.85,setsar=1[b];"
            "[fg]scale=-2:1080:flags=lanczos,setsar=1[f];"
            "[b][f]overlay=(W-w)/2:(H-h)/2,fps=60,format=yuv420p[vout]"
        )
        cmd.extend(["-filter_complex", vf, "-map", "[vout]", "-map", "0:a?"])
    else:
        vf = "scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,fps=60,format=yuv420p"
        cmd.extend(["-vf", vf])

    cmd.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "19",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-ac",
            "2",
            str(out_seg),
        ]
    )
    res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=180)
    if res.returncode != 0 or not out_seg.exists():
        raise RuntimeError(f"Failed normalizing segment {src_mp4}: {res.stderr[-300:]}")
    _, _, _, seg_dur = probe_video_stream(out_seg)
    return seg_dur if seg_dur > 0 else dur


def compile_longform_16_9(
    clip_specs: list[dict[str, Any]],
    out_dir: Path,
    title: str = "Best GTA VI Moments Compilation",
    thumbnail_headline: str = "GTA VI CHAOS",
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Compile a list of clip dicts (`path`, optional `label`/`start_trim`/`end_trim`) into 16:9 YouTube master."""
    if not clip_specs:
        raise ValueError("No clips provided for Long-Form 16:9 compilation")

    out_dir.mkdir(parents=True, exist_ok=True)
    out_mp4 = out_dir / "compilation_16_9_master.mp4"
    chapters_txt = out_dir / "compilation_chapters.txt"
    thumb_jpg = out_dir / "thumbnail_1280x720.jpg"
    meta_json = out_dir / "compilation_meta.json"

    chapters: list[dict[str, Any]] = []
    elapsed = 0.0

    with tempfile.TemporaryDirectory(prefix="compile_16_9_") as tmp:
        tmp_dir = Path(tmp)
        seg_files: list[Path] = []
        for idx, spec in enumerate(clip_specs, start=1):
            src = Path(str(spec["path"]))
            if not src.exists():
                continue
            seg_out = tmp_dir / f"seg_{idx:03d}.mp4"
            dur = _normalize_segment_16_9(src, seg_out, spec.get("start_trim"), spec.get("end_trim"))
            label = str(spec.get("label") or f"Clip #{idx}")
            chapters.append({"index": idx, "timestamp": _fmt_chapter_ts(elapsed), "start_sec": round(elapsed, 2), "label": label})
            elapsed += dur
            seg_files.append(seg_out)

        if not seg_files:
            raise FileNotFoundError("None of the specified clip paths existed on disk")

        concat_list = tmp_dir / "concat.txt"
        concat_list.write_text("\n".join(f"file '{p.as_posix()}'" for p in seg_files) + "\n", encoding="utf-8")

        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list),
            "-c:v",
            "copy",
            "-af",
            "loudnorm=I=-14.0:TP=-1.5:LRA=11.0,alimiter=limit=0.82:level=false",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-movflags",
            "+faststart",
            str(out_mp4),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=300)
        if res.returncode != 0 or not out_mp4.exists():
            raise RuntimeError(f"Compilation concat/mastering failed: {res.stderr[-300:]}")

    chapter_lines = [f"{c['timestamp']} {c['label']}" for c in chapters]
    chapters_txt.write_text("\n".join(chapter_lines) + "\n", encoding="utf-8")
    generate_youtube_thumbnail(Path(str(clip_specs[0]["path"])), thumbnail_headline, thumb_jpg)

    w, h, fps, final_dur = probe_video_stream(out_mp4)
    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "title": title,
        "output_mp4": str(out_mp4),
        "thumbnail_jpg": str(thumb_jpg),
        "chapters_txt": str(chapters_txt),
        "width": w,
        "height": h,
        "fps": fps,
        "duration_sec": round(final_dur, 2),
        "clips_compiled": len(chapters),
        "chapters": chapters,
    }
    meta_json.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    insert_telemetry("longform_compiler", "compilation_rendered", report, db_path=db_path)
    return report


def compile_from_studio_db(
    out_dir: Path,
    streamer_id: str | None = None,
    max_clips: int = 12,
    title: str = "Best GTA VI Moments Compilation",
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Query media-check-approved clips from `runs/studio.db` and compile into a 16:9 YouTube master."""
    rows = list_approved_clips(streamer_id=streamer_id, limit=max_clips, db_path=db_path)
    specs: list[dict[str, Any]] = []
    for r in rows:
        mp4 = Path(str(r.get("output_mp4") or ""))
        if not mp4.exists():
            continue
        label = str(r.get("matched_lore_entity") or f"{r.get('streamer_id')} {r.get('clip_id')}")
        specs.append({"path": str(mp4), "label": label})
    return compile_longform_16_9(specs, out_dir=out_dir, title=title, db_path=db_path)
