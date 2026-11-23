"""Deterministic 9:16 split-screen FFmpeg renderer with 2-pass loudnorm + aresample=48000 (Hito 1.4)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

from src.callout import render_freeze_callout
from src.captions import generate_karaoke_ass
from src.miner import CHANNEL_PROFILE_PATH, RUNS_DIR, enforce_runs_quota
from src.schemas import AnnotatedTranscript, EditPlan

_JSON_BLOCK_RE = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.DOTALL)


def _measure_loudnorm_pass1(source_slice: Path, start_trim: float, duration: float) -> dict[str, str]:
    """Pass 1 loudnorm extraction returning measured_I, measured_TP, measured_LRA, measured_thresh, offset."""
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-ss",
        f"{start_trim:.3f}",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(source_slice.resolve()),
        "-vn",
        "-af",
        "highpass=f=70,loudnorm=I=-14.0:TP=-1.6:LRA=11.0:print_format=json",
        "-f",
        "null",
        "-",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
    m = _JSON_BLOCK_RE.search(res.stderr or "")
    if not m:
        raise RuntimeError(f"Could not parse loudnorm pass-1 JSON: {(res.stderr or '')[-300:]}")
    stats: dict[str, Any] = json.loads(m.group(0))
    return {
        "input_i": str(stats["input_i"]),
        "input_tp": str(stats["input_tp"]),
        "input_lra": str(stats["input_lra"]),
        "input_thresh": str(stats["input_thresh"]),
        "target_offset": str(stats["target_offset"]),
    }


def _build_split_filter(
    webcam_box_px: tuple[int, int, int, int] | None,
    has_callout: bool,
    ff_start: float,
    ff_dur: float,
    loud_stats: dict[str, str],
) -> str:
    if webcam_box_px:
        wx, wy, ww, wh = webcam_box_px
        cam_flt = (
            f"[0:v]setpts=PTS-STARTPTS,crop={ww}:{wh}:{wx}:{wy},"
            f"scale=1080:692:force_original_aspect_ratio=increase,crop=1080:692,setsar=1[top]"
        )
    else:
        cam_flt = (
            "[0:v]setpts=PTS-STARTPTS,crop=iw*0.42:iw*0.42*692/1080:iw*0.29:ih*0.05,"
            "scale=1080:692,setsar=1[top]"
        )

    gp_flt = "[0:v]setpts=PTS-STARTPTS,scale=-2:1228,crop=1080:1228:(iw-1080)/2:0,setsar=1[bot]"
    stack_flt = "[top][bot]vstack=inputs=2,fps=30[base]"

    if has_callout:
        ff_end = ff_start + ff_dur
        ov_flt = (
            f"[1:v]trim=duration={ff_dur:.3f},setpts=PTS-STARTPTS+{ff_start:.3f}/TB[ff];"
            f"[base][ff]overlay=0:0:enable='between(t,{ff_start:.3f},{ff_end:.3f})':eof_action=pass[ov]"
        )
    else:
        ov_flt = "[base]null[ov]"

    sub_flt = "[ov]ass=subs.ass,format=nv12[vout]"

    if loud_stats["input_i"] == "-inf":
        aud_flt = "[0:a]asetpts=PTS-STARTPTS,aresample=48000[aout]"
    else:
        aud_flt = (
            "[0:a]asetpts=PTS-STARTPTS,highpass=f=70,"
            f"loudnorm=I=-14.0:TP=-1.6:LRA=11.0:linear=true:"
            f"measured_I={loud_stats['input_i']}:measured_TP={loud_stats['input_tp']}:"
            f"measured_LRA={loud_stats['input_lra']}:measured_thresh={loud_stats['input_thresh']}:"
            f"offset={loud_stats['target_offset']},aresample=48000,alimiter=limit=0.87:level=false[aout]"
        )

    return f"{cam_flt};{gp_flt};{stack_flt};{ov_flt};{sub_flt};{aud_flt}"


def render_master_clip(
    source_slice: Path,
    edit_plan: EditPlan,
    transcript: AnnotatedTranscript,
    out_mp4: Path,
    compute_profile: str = "local_hp",
) -> Path:
    """Render 1080x1920 H.264/yuv420p vertical clip with 2-pass loudnorm and relative libass cwd."""
    cfg: dict[str, Any] = {}
    if CHANNEL_PROFILE_PATH.exists():
        cfg = yaml.safe_load(CHANNEL_PROFILE_PATH.read_text(encoding="utf-8")) or {}
    captions_cfg = cfg.get("captions") or {}
    auto_cleanup = bool((cfg.get("storage") or {}).get("auto_cleanup_temp", True))

    clip_dur = round(max(1.0, edit_plan.end_trim - edit_plan.start_trim), 3)
    work_dir = out_mp4.parent / f".work_{edit_plan.clip_id}"
    work_dir.mkdir(parents=True, exist_ok=True)
    out_mp4.parent.mkdir(parents=True, exist_ok=True)

    try:
        generate_karaoke_ass(transcript, edit_plan, work_dir / "subs.ass", captions_cfg=captions_cfg)
        callout_png = render_freeze_callout(source_slice, edit_plan, work_dir / "callout.png")
        has_callout = callout_png is not None and callout_png.exists()

        loud_stats = _measure_loudnorm_pass1(source_slice, edit_plan.start_trim, clip_dur)
        print(
            f"[RENDERER] pass1 loudnorm input_i={loud_stats['input_i']} "
            f"input_tp={loud_stats['input_tp']} offset={loud_stats['target_offset']}"
        )

        ff_start = edit_plan.freeze_frame.timestamp_sec if edit_plan.freeze_frame else 0.0
        ff_dur = edit_plan.freeze_frame.duration_sec if edit_plan.freeze_frame else 0.0
        flt = _build_split_filter(edit_plan.webcam_box_px, has_callout, ff_start, ff_dur, loud_stats)

        base_cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{edit_plan.start_trim:.3f}",
            "-t",
            f"{clip_dur:.3f}",
            "-i",
            str(source_slice.resolve()),
        ]
        if has_callout:
            base_cmd.extend(["-loop", "1", "-i", "callout.png"])

        base_cmd.extend([
            "-filter_complex",
            flt,
            "-map",
            "[vout]",
            "-map",
            "[aout]",
            "-t",
            f"{clip_dur:.3f}",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
        ])

        primary_enc = ["-c:v", "h264_nvenc", "-cq", "22"] if compute_profile in {"gcp_spot_l4", "colab_t4"} else ["-c:v", "h264_qsv", "-global_quality", "22"]
        res = subprocess.run(
            base_cmd + primary_enc + [str(out_mp4.resolve())],
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if res.returncode != 0:
            print(f"[RENDERER] primary encoder fallback -> libx264 ({(res.stderr or '')[:140]})")
            fb = subprocess.run(
                base_cmd + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", str(out_mp4.resolve())],
                cwd=str(work_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if fb.returncode != 0:
                raise RuntimeError(f"FFmpeg render failed: {fb.stderr[:300]}")
    finally:
        if auto_cleanup:
            shutil.rmtree(work_dir, ignore_errors=True)
        enforce_runs_quota(RUNS_DIR, protect_dir=out_mp4.parent)

    return out_mp4
