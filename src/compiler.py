"""Owned-Channel YouTube Long-Form 16:9 Compiler (1920x1080, chapters, native 16:9 BiRefNet-lite editorial thumbnail, -14 LUFS)."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.db import DEFAULT_DB_PATH, init_db, insert_telemetry, list_approved_clips
from src.design_engine import render_editorial_thumbnail_16_9
from src.miner import PROJECT_ROOT
from src.renderer import probe_video_stream

SFX_WHOOSH = PROJECT_ROOT / "assets" / "sfx" / "whoosh.wav"


def _fmt_chapter_ts(sec: float) -> str:
    total = max(0, round(sec))
    mins, secs = divmod(total, 60)
    hrs, mins = divmod(mins, 60)
    return f"{hrs:02d}:{mins:02d}:{secs:02d}" if hrs else f"{mins:02d}:{secs:02d}"


def _resolve_native_16_9_thumb_source(
    clip_specs: list[dict[str, Any]],
    thumb_source: Path | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> tuple[Path, tuple[int, int, int, int] | None]:
    """Resolve native 16:9 slice_*.mp4 + optional webcam_box_px (avoids vertical 9:16 pillarbox in thumbnails)."""
    if thumb_source is not None and thumb_source.exists():
        return thumb_source, None

    # 1. Check clip_specs for an explicit horizontal source_slice or native 16:9 clip
    for spec in clip_specs:
        cand_slice = spec.get("source_slice_path")
        if cand_slice and Path(str(cand_slice)).exists():
            wb = spec.get("webcam_box_px")
            return Path(str(cand_slice)), (tuple(wb) if wb and len(wb) == 4 else None)
        p = Path(str(spec["path"]))
        if p.exists():
            w, h, _, _ = probe_video_stream(p)
            if w > h:
                return p, None

    # 2. Look up matching native 16:9 slice in runs/studio.db (`slices.video_path`)
    try:
        init_db(db_path)
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """
                SELECT s.video_path, s.run_id, s.slice_id
                FROM slices s
                JOIN clips c ON c.run_id = s.run_id AND c.slice_id = s.slice_id
                WHERE c.media_check_passed = 1 AND s.video_path != ''
                ORDER BY c.critic_overall DESC, c.updated_at DESC
                LIMIT 1
                """
            ).fetchone()
            if row and Path(str(row["video_path"])).exists():
                slice_p = Path(str(row["video_path"]))
                wb_px = _lookup_webcam_box_in_state(slice_p.parent / "state.json", str(row["slice_id"]))
                return slice_p, wb_px
        finally:
            conn.close()
    except Exception:
        pass

    return Path(str(clip_specs[0]["path"])), None


def _lookup_webcam_box_in_state(state_path: Path, slice_id: str) -> tuple[int, int, int, int] | None:
    if not state_path.is_file():
        return None
    try:
        st = json.loads(state_path.read_text(encoding="utf-8"))
        for a in st.get("slice_audits") or []:
            if a.get("slice_id") == slice_id and a.get("webcam_box_px"):
                wb = a["webcam_box_px"]
                if len(wb) == 4:
                    return (int(wb[0]), int(wb[1]), int(wb[2]), int(wb[3]))
    except Exception:
        pass
    return None


def generate_youtube_thumbnail(
    source_video: Path,
    headline: str,
    out_jpg: Path,
    badge_text: str = "GTA VI",
    webcam_box_px: tuple[int, int, int, int] | None = None,
) -> dict[str, Any]:
    """Generate an editorial 1280x720 YouTube Long-Form thumbnail via DesignEngine (BiRefNet-lite + Chrome Headless)."""
    return render_editorial_thumbnail_16_9(
        video_or_img=source_video,
        headline=headline,
        out_jpg=out_jpg,
        badge_text=badge_text,
        webcam_box_px=webcam_box_px,
    )


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
    thumb_source: Path | None = None,
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
    resolved_thumb_src, resolved_cam = _resolve_native_16_9_thumb_source(clip_specs, thumb_source=thumb_source, db_path=db_path)
    thumb_meta = generate_youtube_thumbnail(
        resolved_thumb_src,
        thumbnail_headline,
        thumb_jpg,
        webcam_box_px=resolved_cam,
    )

    w, h, fps, final_dur = probe_video_stream(out_mp4)
    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "title": title,
        "output_mp4": str(out_mp4),
        "thumbnail_jpg": str(thumb_jpg),
        "thumbnail_source": str(resolved_thumb_src),
        "thumbnail_meta": thumb_meta,
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
    thumbnail_headline: str = "GTA VI CHAOS",
    thumb_source: Path | None = None,
    db_path: Path = DEFAULT_DB_PATH,
    strict_clean_only: bool = True,
) -> dict[str, Any]:
    """Compile approved clips from `runs/studio.db` using native `16:9` `slices.video_path` + `start_trim`/`end_trim`."""
    rows = list_approved_clips(streamer_id=streamer_id, limit=max_clips * 2, db_path=db_path)
    slice_map: dict[tuple[str, str], dict[str, Any]] = {}
    init_db(db_path)
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        for sr in conn.execute("SELECT run_id, slice_id, video_path, dmca_flag FROM slices").fetchall():
            slice_map[(str(sr["run_id"]), str(sr["slice_id"]))] = dict(sr)
    finally:
        conn.close()

    specs: list[dict[str, Any]] = []
    for r in rows:
        if len(specs) >= max_clips:
            break
        run_id = str(r.get("run_id") or "")
        slice_id = str(r.get("slice_id") or "")
        s_info = slice_map.get((run_id, slice_id), {})
        if strict_clean_only:
            if int(s_info.get("dmca_flag") or 0) == 1:
                continue
            pkg_raw = str(r.get("packaging_json") or "")
            if pkg_raw:
                try:
                    pkg_obj = json.loads(pkg_raw)
                    if str(pkg_obj.get("audio_policy") or "strict_clean") not in {"strict_clean", ""}:
                        continue
                except Exception:
                    pass

        label = str(r.get("matched_lore_entity") or f"{r.get('streamer_id')} {r.get('clip_id')}")
        native_slice = Path(str(s_info.get("video_path") or ""))
        s_trim = float(r.get("start_trim") or 0.0)
        e_trim = float(r.get("end_trim") or 0.0)
        if s_info.get("video_path") and native_slice.is_file() and e_trim > s_trim:
            wb_px = _lookup_webcam_box_in_state(native_slice.parent / "state.json", slice_id)
            specs.append(
                {
                    "path": str(native_slice),
                    "source_slice_path": str(native_slice),
                    "start_trim": s_trim,
                    "end_trim": e_trim,
                    "webcam_box_px": wb_px,
                    "label": label,
                }
            )
            continue

        mp4 = Path(str(r.get("output_mp4") or ""))
        if mp4.is_file():
            specs.append({"path": str(mp4), "label": label})

    return compile_longform_16_9(
        specs,
        out_dir=out_dir,
        title=title,
        thumbnail_headline=thumbnail_headline,
        thumb_source=thumb_source,
        db_path=db_path,
    )
