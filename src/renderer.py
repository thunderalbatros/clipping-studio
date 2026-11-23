"""Senior Studio 9:16 renderer: adaptive layout, safe-zone webcam anchor, selective jump-cuts, RMS punch-in zoom, 0-7s profanity bleeper, CC0 SFX, and 2-pass loudnorm (Fase A)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import wave
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from src.callout import render_freeze_callout
from src.captions import generate_karaoke_ass, remap_time_through_segments
from src.miner import (
    CHANNEL_PROFILE_PATH,
    PROJECT_ROOT,
    RUNS_DIR,
    enforce_runs_quota,
    probe_video_stream,
)
from src.resource_guard import apply_thread_budget
from src.schemas import AnnotatedTranscript, EditPlan, WordTimestamp
from src.speaker_guard import detect_face_center_ultraface

_JSON_BLOCK_RE = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.DOTALL)
_FONTSELECT_RE = re.compile(r"fontselect:\s*\(([^)]+)\)", re.IGNORECASE)
_PROFANITY_RE = re.compile(r"\b(fuck\w*|shit\w*|bitch\w*|cunt\w*|nigg\w*|whore\w*|slut\w*|motherfuck\w*)\b", re.IGNORECASE)
BUNDLED_FONT_PATH = PROJECT_ROOT / "assets" / "fonts" / "Montserrat-Black.ttf"
SFX_DIR = PROJECT_ROOT / "assets" / "sfx"


def _even(n: int) -> int:
    return max(2, int(n) & ~1)


def rects_overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    """Return True if [x1, y1, x2, y2] rectangles have positive intersection area."""
    return not (a[2] <= b[0] or a[0] >= b[2] or a[3] <= b[1] or a[1] >= b[3])


def compute_webcam_card_pos(card_w: int, card_h: int) -> tuple[int, int]:
    """Anchor rounded webcam card cleanly below TikTok/Reels y=180 top bar and centered inside top 692px band."""
    px = (1080 - card_w) // 2
    py = max(0, min(692 - card_h - 10, max(172, 692 - card_h - 16)))
    return px, py


def find_opening_profanity_spans(
    words: list[WordTimestamp],
    start_trim: float,
    kept_segments: list[tuple[float, float]] | None = None,
    shield_sec: float = 7.5,
) -> list[tuple[float, float]]:
    """Locate explicit profanity timestamps in the opening 0..7.5s window for YT/FB monetization bleeping."""
    spans: list[tuple[float, float]] = []
    for w in words:
        if w.end <= start_trim or w.start >= start_trim + shield_sec + 4.0:
            continue
        if not _PROFANITY_RE.search(w.word):
            continue
        ms = remap_time_through_segments(max(0.0, w.start - start_trim), kept_segments)
        me = remap_time_through_segments(max(0.05, w.end - start_trim), kept_segments)
        if ms is None or me is None or ms > shield_sec:
            continue
        s = round(max(0.0, ms - 0.03), 3)
        e = round(max(s + 0.14, me + 0.03), 3)
        if spans and s <= spans[-1][1] + 0.08:
            spans[-1] = (spans[-1][0], max(spans[-1][1], e))
        else:
            spans.append((s, e))
    return spans


def _extract_clip_pcm_16k(source_slice: Path, start_trim: float, clip_dur: float) -> np.ndarray | None:
    with tempfile.TemporaryDirectory(prefix="cs_rms_") as tmp:
        wav_p = Path(tmp) / "clip.wav"
        res = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{start_trim:.3f}",
                "-t",
                f"{clip_dur:.3f}",
                "-i",
                str(source_slice.resolve()),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(wav_p),
            ],
            capture_output=True,
            check=False,
        )
        if res.returncode != 0 or not wav_p.exists():
            return None
        with wave.open(str(wav_p), "rb") as wf:
            raw = wf.readframes(wf.getnframes())
    if not raw:
        return None
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def compute_jump_cut_segments(
    clip_dur: float,
    start_trim: float,
    transcript: AnnotatedTranscript,
    pcm_16k: np.ndarray | None = None,
    cut_silence_over_sec: float = 0.65,
    preserve_pause_sec: float = 0.25,
    max_cuts: int = 3,
) -> list[tuple[float, float]]:
    """Trim only genuine low-energy dead air (>0.95s, <-30 dBFS, no action/SFX) while preserving Synchresis pauses."""
    min_gap = max(0.95, cut_silence_over_sec + 0.30)
    in_clip = sorted(
        [
            (max(0.0, w.start - start_trim), min(clip_dur, w.end - start_trim))
            for w in transcript.words
            if w.end > start_trim and w.start < start_trim + clip_dur and not w.is_npc_or_other
        ],
        key=lambda x: x[0],
    )
    if len(in_clip) < 3:
        return [(0.0, round(clip_dur, 3))]

    protected_events = [
        (max(0.0, ev.start - start_trim), min(clip_dur, ev.end - start_trim))
        for ev in transcript.non_verbal_events
        if ev.kind in {"shouting", "laughter", "visual_motion"}
        and ev.end > start_trim
        and ev.start < start_trim + clip_dur
    ]

    cut_spans: list[tuple[float, float]] = []
    for i in range(len(in_clip) - 1):
        g_start = in_clip[i][1] + 0.12
        g_end = in_clip[i + 1][0] - preserve_pause_sec
        if g_end - g_start < min_gap:
            continue
        # Never cut across the final 3.5s climax/payoff (e.g., WASTED screen) or opening 1.5s
        if g_start < 1.5 or g_end > clip_dur - 3.5:
            continue
        if any(not (g_end <= ps or g_start >= pe) for ps, pe in protected_events):
            continue
        # Verify true acoustic silence (< -30 dBFS) so explosions/gunshots/game SFX are preserved intact
        if pcm_16k is not None and len(pcm_16k) > 0:
            s_idx = max(0, min(len(pcm_16k) - 1, int(g_start * 16000)))
            e_idx = max(s_idx + 1, min(len(pcm_16k), int(g_end * 16000)))
            seg = pcm_16k[s_idx:e_idx]
            rms = float(np.sqrt(np.mean(seg * seg)) + 1e-9)
            db = 20.0 * float(np.log10(rms))
            if db > -30.0:
                continue
        cut_spans.append((round(g_start, 3), round(g_end, 3)))
        if len(cut_spans) >= max_cuts:
            break

    if not cut_spans:
        return [(0.0, round(clip_dur, 3))]

    kept: list[tuple[float, float]] = []
    cursor = 0.0
    for cs, ce in cut_spans:
        if cs > cursor + 0.2:
            kept.append((round(cursor, 3), round(cs, 3)))
        cursor = ce
    if clip_dur > cursor + 0.2:
        kept.append((round(cursor, 3), round(clip_dur, 3)))

    # Safety: never shorten clip below 15.0s
    total_kept = sum(e - s for s, e in kept)
    if total_kept < 15.0 or len(kept) <= 1:
        return [(0.0, round(clip_dur, 3))]
    return kept


def find_auto_editor_executable() -> Path | None:
    """Locate auto-editor CLI inside project .venv or PATH."""
    venv_ae = PROJECT_ROOT / ".venv" / "Scripts" / "auto-editor.exe"
    if venv_ae.is_file():
        return venv_ae
    found = shutil.which("auto-editor")
    return Path(found) if found else None


def detect_segments_auto_editor(
    source_slice: Path,
    start_trim: float,
    clip_dur: float,
    transcript: AnnotatedTranscript,
    pcm_16k: np.ndarray | None = None,
    margin_sec: float = 0.25,
    threshold_pct: float = 4.0,
    min_cut_sec: float = 0.95,
    max_cuts: int = 3,
) -> tuple[list[tuple[float, float]] | None, dict[str, Any]]:
    """Run WyattBlue/auto-editor (`--export v3`) for dead-air detection with gaming Synchresis guardrails.

    Preserves:
    - `margin_sec=0.25s` Synchresis breathing room around every loud event.
    - Dramatic pauses `< 0.95s` (never removes brief comedic beats).
    - Opening `1.5s` (action hook) and closing `3.5s` (climax / seamless loop).
    - Protected non-verbal events (`shouting`, `laughter`, `visual_motion`) and loud game SFX (`> -30 dBFS`).
    - Rejects over-aggressive edits (>35% cut or <15.0s kept) so caller cleanly falls back to PCM analyzer.
    """
    ae_bin = find_auto_editor_executable()
    if ae_bin is None or clip_dur < 16.0:
        return None, {"provider": "auto_editor", "used": False, "reason": "binary_missing_or_short_clip"}

    protected_events = [
        (max(0.0, ev.start - start_trim), min(clip_dur, ev.end - start_trim))
        for ev in transcript.non_verbal_events
        if ev.kind in {"shouting", "laughter", "visual_motion"}
        and ev.end > start_trim
        and ev.start < start_trim + clip_dur
    ]

    with tempfile.TemporaryDirectory(prefix="cs_ae_") as tmp:
        tmp_dir = Path(tmp)
        clip_wav = tmp_dir / "clip.wav"
        v3_json = tmp_dir / "timeline.v3"

        ext_res = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{start_trim:.3f}",
                "-t",
                f"{clip_dur:.3f}",
                "-i",
                str(source_slice.resolve()),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "48000",
                "-c:a",
                "pcm_s16le",
                str(clip_wav),
            ],
            capture_output=True,
            check=False,
            timeout=25,
        )
        if ext_res.returncode != 0 or not clip_wav.is_file():
            return None, {"provider": "auto_editor", "used": False, "reason": "wav_extract_failed"}

        ae_res = subprocess.run(
            [
                str(ae_bin),
                str(clip_wav),
                "--margin",
                f"{margin_sec:.2f}s",
                "--edit",
                f"audio:threshold={threshold_pct:g}%",
                "--export",
                "v3",
                "-o",
                str(v3_json),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if ae_res.returncode != 0 or not v3_json.is_file():
            return None, {"provider": "auto_editor", "used": False, "reason": f"ae_exit_{ae_res.returncode}"}

        try:
            tl = json.loads(v3_json.read_text(encoding="utf-8"))
            tb = float(Fraction(str(tl.get("timebase", "30"))))
            tracks = tl.get("a") or tl.get("v") or []
            raw_clips = tracks[0] if tracks and isinstance(tracks[0], list) else []
        except Exception as exc:
            return None, {"provider": "auto_editor", "used": False, "reason": f"v3_parse_error:{exc}"}

    if not raw_clips or tb <= 0:
        return [(0.0, round(clip_dur, 3))], {"provider": "auto_editor", "used": True, "cuts_applied": 0}

    # Reconstruct candidate silent gaps between kept auto-editor chunks
    ae_kept = sorted(
        [(float(c.get("offset", 0)) / tb, (float(c.get("offset", 0)) + float(c.get("dur", 0))) / tb) for c in raw_clips],
        key=lambda x: x[0],
    )
    approved_cuts: list[tuple[float, float]] = []
    for i in range(len(ae_kept) - 1):
        g_start = max(0.0, ae_kept[i][1])
        g_end = min(clip_dur, ae_kept[i + 1][0])
        if g_end - g_start < min_cut_sec:
            continue
        if g_start < 1.5 or g_end > clip_dur - 3.5:
            continue
        if any(not (g_end <= ps or g_start >= pe) for ps, pe in protected_events):
            continue
        if pcm_16k is not None and len(pcm_16k) > 0:
            s_idx = max(0, min(len(pcm_16k) - 1, int(g_start * 16000)))
            e_idx = max(s_idx + 1, min(len(pcm_16k), int(g_end * 16000)))
            seg = pcm_16k[s_idx:e_idx]
            rms = float(np.sqrt(np.mean(seg * seg)) + 1e-9)
            db = 20.0 * float(np.log10(rms))
            if db > -30.0:
                continue
        approved_cuts.append((round(g_start, 3), round(g_end, 3)))
        if len(approved_cuts) >= max_cuts:
            break

    if not approved_cuts:
        return [(0.0, round(clip_dur, 3))], {
            "provider": "auto_editor",
            "used": True,
            "raw_ae_chunks": len(ae_kept),
            "cuts_applied": 0,
            "margin_sec": margin_sec,
        }

    kept: list[tuple[float, float]] = []
    cursor = 0.0
    for cs, ce in approved_cuts:
        if cs > cursor + 0.2:
            kept.append((round(cursor, 3), round(cs, 3)))
        cursor = ce
    if clip_dur > cursor + 0.2:
        kept.append((round(cursor, 3), round(clip_dur, 3)))

    total_kept = sum(e - s for s, e in kept)
    removed_ratio = 1.0 - (total_kept / max(1.0, clip_dur))
    if total_kept < 15.0 or removed_ratio > 0.35 or len(kept) <= 1:
        return None, {
            "provider": "auto_editor",
            "used": False,
            "reason": f"over_aggressive_fallback(removed={removed_ratio:.2f},kept={total_kept:.1f}s)",
        }

    return kept, {
        "provider": "auto_editor",
        "used": True,
        "raw_ae_chunks": len(ae_kept),
        "cuts_applied": len(approved_cuts),
        "removed_sec": round(clip_dur - total_kept, 2),
        "margin_sec": margin_sec,
    }


def render_ab_hook_variant(
    master_mp4: Path,
    alt_hook_text: str,
    out_mp4: Path,
    duration_sec: float = 3.0,
    compute_profile: str = "local_hp",
) -> Path:
    """Render Variant B MP4 (0-3s alternative hook banner + fresh re-encode hash) for IG Trial Reels / TikTok A/B."""
    apply_thread_budget()
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    clean_hook = " ".join(alt_hook_text.strip().upper().split()[:6]) or "WAIT FOR THIS"

    with tempfile.TemporaryDirectory(prefix="cs_ab_var_") as tmp:
        work_dir = Path(tmp)
        banner_png = work_dir / "hook_banner.png"
        ov = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
        draw = ImageDraw.Draw(ov)

        font_path = str(BUNDLED_FONT_PATH) if BUNDLED_FONT_PATH.is_file() else None
        font_sz = 54 if len(clean_hook) <= 18 else 44
        font = ImageFont.truetype(font_path, font_sz) if font_path else ImageFont.load_default()
        bbox = draw.textbbox((0, 0), clean_hook, font=font, stroke_width=4)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        pad_x, pad_y = 34, 18
        box_w = min(900, tw + 2 * pad_x)
        box_h = th + 2 * pad_y
        # Anchor cleanly inside Universal Safe Box seam (y ~ 685) so webcam eyes and captions stay unobstructed
        bx1 = (1080 - box_w) // 2
        by1 = 682
        bx2, by2 = bx1 + box_w, by1 + box_h
        draw.rounded_rectangle([bx1, by1, bx2, by2], radius=18, fill=(10, 12, 18, 224), outline=(255, 212, 0, 235), width=4)
        tx = (1080 - tw) // 2
        ty = by1 + pad_y - bbox[1]
        draw.text((tx, ty), clean_hook, font=font, fill=(255, 224, 64, 255), stroke_width=3, stroke_fill=(6, 8, 14, 255))
        ov.save(banner_png, format="PNG")

        start_s = 0.05
        end_s = max(0.8, min(4.0, duration_sec))
        flt = (
            f"[1:v]trim=duration={end_s:.3f},setpts=PTS-STARTPTS+{start_s:.3f}/TB[ov];"
            f"[0:v][ov]overlay=0:0:enable='between(t,{start_s:.3f},{start_s + end_s:.3f})':eof_action=pass,format=nv12[vout]"
        )
        base_cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(master_mp4.resolve()),
            "-loop",
            "1",
            "-i",
            "hook_banner.png",
            "-filter_complex",
            flt,
            "-map",
            "[vout]",
            "-map",
            "0:a?",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-ar",
            "48000",
            "-movflags",
            "+faststart",
        ]
        enc = (
            ["-c:v", "h264_nvenc", "-cq", "19"]
            if compute_profile in {"gcp_spot_l4", "colab_t4"}
            else ["-c:v", "h264_qsv", "-global_quality", "18", "-look_ahead", "0"]
        )
        res = subprocess.run(base_cmd + enc + [str(out_mp4.resolve())], cwd=str(work_dir), capture_output=True, text=True, check=False)
        if res.returncode != 0 or not out_mp4.exists():
            fb = subprocess.run(
                base_cmd + ["-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p", str(out_mp4.resolve())],
                cwd=str(work_dir),
                capture_output=True,
                text=True,
                check=False,
            )
            if fb.returncode != 0:
                raise RuntimeError(f"A/B variant render failed: {fb.stderr[-300:]}")
    return out_mp4


def find_punch_in_windows(
    transcript: AnnotatedTranscript,
    start_trim: float,
    out_dur: float,
    kept_segments: list[tuple[float, float]] | None,
    freeze_ts: float | None = None,
    max_windows: int = 2,
) -> list[tuple[float, float]]:
    """Select up to 2 short motivated 1.10x punch-in zoom windows on vocal/laughter peaks or freeze impact."""
    cands: list[tuple[float, float, float]] = []
    for ev in transcript.non_verbal_events:
        if ev.kind not in {"shouting", "laughter"}:
            continue
        ms = remap_time_through_segments(max(0.0, ev.start - start_trim), kept_segments)
        if ms is None or ms < 1.2 or ms > out_dur - 2.0:
            continue
        win_s = round(max(1.0, ms - 0.15), 3)
        win_e = round(min(out_dur - 0.5, win_s + 1.6), 3)
        if win_e - win_s >= 0.8:
            cands.append((ev.intensity, win_s, win_e))

    if freeze_ts is not None and 1.8 <= freeze_ts <= out_dur - 1.5:
        win_s = round(max(1.0, freeze_ts - 1.3), 3)
        win_e = round(min(out_dur - 0.4, freeze_ts + 0.15), 3)
        cands.append((0.95, win_s, win_e))

    cands.sort(key=lambda x: x[0], reverse=True)
    chosen: list[tuple[float, float]] = []
    for _, ws, we in cands:
        if all(we + 3.0 < cs or ws > ce + 3.0 for cs, ce in chosen):
            chosen.append((ws, we))
            if len(chosen) >= max_windows:
                break
    chosen.sort(key=lambda x: x[0])
    return chosen


def compute_adaptive_layout(
    src_w: int,
    src_h: int,
    webcam_box_px: tuple[int, int, int, int] | None,
    face_box_norm: tuple[float, float, float, float] | None = None,
    max_upscale: float = 1.8,
    exclusion_pad: int = 48,
) -> dict[str, Any]:
    """Compute non-overlapping gameplay crop and anti-upscale webcam framing geometry."""
    if not webcam_box_px:
        target_ar = 1080.0 / 1920.0
        gw = _even(min(src_w, round(src_h * target_ar)))
        gh = _even(src_h)
        gx = _even((src_w - gw) // 2)
        return {
            "layout_mode": "full_gameplay_9_16",
            "webcam_crop_px": None,
            "webcam_card_size": None,
            "webcam_upscale_factor": 0.0,
            "is_framed_card": False,
            "gameplay_crop_px": (gx, 0, gw, gh),
            "forbidden_webcam_rect": None,
            "no_webcam_gameplay_overlap": True,
        }

    wx, wy, ww, wh = webcam_box_px
    inset = 6 if (ww >= 180 and wh >= 140) else 0
    cam_x = max(0, min(src_w - 32, wx + inset))
    cam_y = max(0, min(src_h - 32, wy + inset))
    cam_w = _even(max(32, min(src_w - cam_x, ww - 2 * inset)))
    cam_h = _even(max(32, min(src_h - cam_y, wh - 2 * inset)))

    if face_box_norm is not None:
        fx1, fy1, fx2, fy2 = face_box_norm
        fcx = cam_x + round(((fx1 + fx2) * 0.5) * cam_w)
        fcy = cam_y + round(((fy1 + fy2) * 0.5) * cam_h)
        sub_w = _even(max(64, round(cam_w * 0.92)))
        sub_h = _even(max(64, round(cam_h * 0.92)))
        cam_x = max(wx + inset, min(wx + ww - inset - sub_w, fcx - sub_w // 2)) & ~1
        cam_y = max(wy + inset, min(wy + wh - inset - sub_h, fcy - sub_h // 2)) & ~1
        cam_w, cam_h = sub_w, sub_h

    if cam_w >= 540:
        card_w, card_h = 1080, 692
        upscale = round(1080.0 / float(cam_w), 2)
        is_framed_card = False
    else:
        # Cap height <= 496px so top edge starts cleanly below TikTok/Reels y=180 bar
        scale = min(max_upscale, 620.0 / float(cam_w), 496.0 / float(cam_h))
        card_w = _even(round(cam_w * scale))
        card_h = _even(round(cam_h * scale))
        upscale = round(card_w / float(cam_w), 2)
        is_framed_card = True

    forb = (
        max(0, wx - exclusion_pad),
        max(0, wy - exclusion_pad),
        min(src_w, wx + ww + exclusion_pad),
        min(src_h, wy + wh + exclusion_pad),
    )

    target_ar = 1080.0 / 1228.0
    ideal_gw = _even(round(src_h * target_ar))
    cam_center_x = wx + ww * 0.5

    if cam_center_x < src_w * 0.5:
        avail_w = src_w - forb[2]
        if avail_w >= ideal_gw:
            gw, gh = ideal_gw, _even(src_h)
            ideal_x = (src_w - gw) // 2
            gx = _even(max(forb[2], min(src_w - gw, ideal_x)))
            gy = 0
        else:
            gw = _even(max(200, avail_w))
            gh = _even(min(src_h, round(gw / target_ar)))
            gx = _even(forb[2])
            gy = _even((src_h - gh) // 2)
    else:
        avail_w = forb[0]
        if avail_w >= ideal_gw:
            gw, gh = ideal_gw, _even(src_h)
            ideal_x = (src_w - gw) // 2
            gx = _even(max(0, min(forb[0] - gw, ideal_x)))
            gy = 0
        else:
            gw = _even(max(200, avail_w))
            gh = _even(min(src_h, round(gw / target_ar)))
            gx = 0
            gy = _even((src_h - gh) // 2)

    gp_rect = (gx, gy, gx + gw, gy + gh)
    no_overlap = not rects_overlap(gp_rect, forb)
    if not no_overlap:
        top_space = forb[1]
        bot_space = src_h - forb[3]
        if top_space >= bot_space and top_space >= 240:
            gh = _even(top_space)
            gw = _even(min(src_w, round(gh * target_ar)))
            gx, gy = _even((src_w - gw) // 2), 0
        else:
            gh = _even(max(240, bot_space))
            gw = _even(min(src_w, round(gh * target_ar)))
            gx, gy = _even((src_w - gw) // 2), _even(forb[3])
        gp_rect = (gx, gy, gx + gw, gy + gh)
        no_overlap = not rects_overlap(gp_rect, forb)

    return {
        "layout_mode": "split_webcam_top",
        "webcam_crop_px": (cam_x, cam_y, cam_w, cam_h),
        "webcam_card_size": (card_w, card_h),
        "webcam_upscale_factor": upscale,
        "is_framed_card": is_framed_card,
        "gameplay_crop_px": (gx, gy, gw, gh),
        "forbidden_webcam_rect": forb,
        "no_webcam_gameplay_overlap": no_overlap,
    }


def _create_webcam_card_assets(card_w: int, card_h: int, work_dir: Path) -> tuple[Path, Path]:
    """Generate rounded alpha mask (card_w x card_h) and soft drop-shadow frame (1080 x 692) via Pillow."""
    radius = 26
    mask = Image.new("L", (card_w, card_h), 0)
    mdraw = ImageDraw.Draw(mask)
    mdraw.rounded_rectangle([0, 0, card_w - 1, card_h - 1], radius=radius, fill=255)
    mask_path = work_dir / "cam_mask.png"
    mask.save(mask_path, format="PNG")

    px, py = compute_webcam_card_pos(card_w, card_h)
    shadow = Image.new("RGBA", (1080, 692), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shadow)
    sdraw.rounded_rectangle([px - 4, py + 8, px + card_w + 4, py + card_h + 14], radius=radius + 4, fill=(0, 0, 0, 175))
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=16))

    cut = Image.new("L", (1080, 692), 255)
    ImageDraw.Draw(cut).rounded_rectangle([px, py, px + card_w - 1, py + card_h - 1], radius=radius, fill=0)
    r, g, b, a = shadow.split()
    a_masked = Image.composite(a, Image.new("L", (1080, 692), 0), cut)
    frame = Image.merge("RGBA", (r, g, b, a_masked))
    fdraw = ImageDraw.Draw(frame)
    fdraw.rounded_rectangle([px, py, px + card_w - 1, py + card_h - 1], radius=radius, outline=(245, 248, 255, 165), width=4)
    fdraw.rectangle([0, 688, 1080, 692], fill=(10, 12, 16, 235))
    frame_path = work_dir / "cam_frame.png"
    frame.save(frame_path, format="PNG")
    return mask_path, frame_path


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


def _build_adaptive_filter(
    layout: dict[str, Any],
    has_callout: bool,
    ff_start: float,
    ff_dur: float,
    loud_stats: dict[str, str],
    callout_idx: int,
    mask_idx: int,
    frame_idx: int,
    kept_segments: list[tuple[float, float]],
    punch_windows: list[tuple[float, float]],
    punch_zoom: float,
    bleep_spans: list[tuple[float, float]],
    sfx_inputs: list[tuple[int, int, float]],
    out_dur: float,
) -> str:
    gx, gy, gw, gh = layout["gameplay_crop_px"]
    parts: list[str] = []

    # Step 1: Optional selective dead-air jump-cut concat preamble
    if len(kept_segments) > 1:
        v_segs: list[str] = []
        a_segs: list[str] = []
        for idx, (s, e) in enumerate(kept_segments):
            parts.append(f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[vj{idx}]")
            parts.append(f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[aj{idx}]")
            v_segs.append(f"[vj{idx}]")
            a_segs.append(f"[aj{idx}]")
        interleaved = "".join(v_segs[i] + a_segs[i] for i in range(len(kept_segments)))
        parts.append(f"{interleaved}concat=n={len(kept_segments)}:v=1:a=1[vsrc][asrc]")
    else:
        parts.append("[0:v]setpts=PTS-STARTPTS[vsrc]")
        parts.append("[0:a]asetpts=PTS-STARTPTS[asrc]")

    # Step 2: Adaptive 9:16 video layout + optional motivated RMS punch-in zoom
    if layout["layout_mode"] == "full_gameplay_9_16":
        if punch_windows and punch_zoom > 1.02:
            zw, zh = _even(round(gw / punch_zoom)), _even(round(gh / punch_zoom))
            zx, zy = _even(gx + (gw - zw) // 2), _even(gy + (gh - zh) // 2)
            en_expr = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in punch_windows)
            parts.append("[vsrc]split=2[vs1][vs2]")
            parts.append(f"[vs1]crop={gw}:{gh}:{gx}:{gy},scale=1080:1920:flags=lanczos,setsar=1[bnorm]")
            parts.append(f"[vs2]crop={zw}:{zh}:{zx}:{zy},scale=1080:1920:flags=lanczos,setsar=1[bzoom]")
            parts.append(f"[bnorm][bzoom]overlay=0:0:enable='{en_expr}':eof_action=pass[base]")
        else:
            parts.append(f"[vsrc]crop={gw}:{gh}:{gx}:{gy},scale=1080:1920:flags=lanczos,setsar=1[base]")
    else:
        cx, cy, cw, ch = layout["webcam_crop_px"]
        card_w, card_h = layout["webcam_card_size"]
        need_bot_zoom = bool(punch_windows and punch_zoom > 1.02)
        n_splits = (3 if layout["is_framed_card"] else 2) + (1 if need_bot_zoom else 0)
        split_labels = [f"[vs{i}]" for i in range(n_splits)]
        parts.append(f"[vsrc]split={n_splits}{''.join(split_labels)}")

        s_ptr = 0
        if layout["is_framed_card"]:
            px, py = compute_webcam_card_pos(card_w, card_h)
            parts.append(
                f"{split_labels[s_ptr]}crop={gw}:{gh}:{gx}:{gy},"
                f"scale=1080:692:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:692,"
                f"gblur=sigma=24,eq=brightness=-0.12,setsar=1[top_bg]"
            )
            s_ptr += 1
            parts.append(
                f"{split_labels[s_ptr]}crop={cw}:{ch}:{cx}:{cy},"
                f"scale={card_w}:{card_h}:flags=lanczos,unsharp=3:3:0.35,format=rgba,setsar=1[cam_raw]"
            )
            s_ptr += 1
            parts.append(f"[{mask_idx}:v]format=gray[cmask];[cam_raw][cmask]alphamerge[cam_round]")
            parts.append(f"[top_bg][cam_round]overlay={px}:{py}:shortest=1[top_mid]")
            parts.append(f"[top_mid][{frame_idx}:v]overlay=0:0:shortest=1[top]")
        else:
            parts.append(
                f"{split_labels[s_ptr]}crop={cw}:{ch}:{cx}:{cy},"
                f"scale=1080:692:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:692,"
                f"unsharp=3:3:0.25,setsar=1[top]"
            )
            s_ptr += 1

        if need_bot_zoom:
            zw, zh = _even(round(gw / punch_zoom)), _even(round(gh / punch_zoom))
            zx, zy = _even(gx + (gw - zw) // 2), _even(gy + (gh - zh) // 2)
            en_expr = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in punch_windows)
            parts.append(f"{split_labels[s_ptr]}crop={gw}:{gh}:{gx}:{gy},scale=1080:1228:flags=lanczos,setsar=1[bot_norm]")
            s_ptr += 1
            parts.append(f"{split_labels[s_ptr]}crop={zw}:{zh}:{zx}:{zy},scale=1080:1228:flags=lanczos,setsar=1[bot_zoom]")
            parts.append(f"[bot_norm][bot_zoom]overlay=0:0:enable='{en_expr}':eof_action=pass[bot]")
        else:
            parts.append(f"{split_labels[s_ptr]}crop={gw}:{gh}:{gx}:{gy},scale=1080:1228:flags=lanczos,setsar=1[bot]")

        parts.append("[top][bot]vstack=inputs=2[base]")

    # Step 3: Freeze-frame callout + Montserrat Black subtitles
    if has_callout:
        ff_end = ff_start + ff_dur
        parts.append(
            f"[{callout_idx}:v]trim=duration={ff_dur:.3f},setpts=PTS-STARTPTS+{ff_start:.3f}/TB[ff];"
            f"[base][ff]overlay=0:0:enable='between(t,{ff_start:.3f},{ff_end:.3f})':eof_action=pass[ov]"
        )
    else:
        parts.append("[base]null[ov]")

    parts.append("[ov]ass=subs.ass:fontsdir=fonts,format=nv12[vout]")

    # Step 4: Audio chain — opening 0..7.5s profanity bleeper + CC0 SFX mix + 2-pass loudnorm + alimiter
    cur_a = "[asrc]"
    mix_inputs: list[str] = []
    if bleep_spans:
        en_bleep = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in bleep_spans)
        parts.append(f"{cur_a}aresample=48000,aformat=channel_layouts=stereo,volume=0:enable='{en_bleep}'[a_clean]")
        parts.append(
            f"aevalsrc=0.14*sin(2*PI*1000*t):s=48000:c=stereo:d={out_dur:.3f},"
            f"volume=0:enable='not({en_bleep})'[a_bleep]"
        )
        cur_a = "[a_clean]"
        mix_inputs.append("[a_bleep]")
    else:
        parts.append(f"{cur_a}aresample=48000,aformat=channel_layouts=stereo[a_clean]")
        cur_a = "[a_clean]"

    for sfx_i, (inp_idx, delay_ms, gain) in enumerate(sfx_inputs):
        lbl = f"[sfx{sfx_i}]"
        parts.append(
            f"[{inp_idx}:a]aresample=48000,aformat=channel_layouts=stereo,"
            f"volume={gain:.3f},adelay={delay_ms}|{delay_ms}{lbl}"
        )
        mix_inputs.append(lbl)

    if mix_inputs:
        all_mix = cur_a + "".join(mix_inputs)
        parts.append(f"{all_mix}amix=inputs={1 + len(mix_inputs)}:duration=first:dropout_transition=0:normalize=0[amixed]")
        cur_a = "[amixed]"

    if loud_stats["input_i"] == "-inf":
        parts.append(f"{cur_a}aresample=48000[aout]")
    else:
        parts.append(
            f"{cur_a}highpass=f=70,"
            f"loudnorm=I=-14.0:TP=-1.6:LRA=11.0:linear=true:"
            f"measured_I={loud_stats['input_i']}:measured_TP={loud_stats['input_tp']}:"
            f"measured_LRA={loud_stats['input_lra']}:measured_thresh={loud_stats['input_thresh']}:"
            f"offset={loud_stats['target_offset']},aresample=48000,alimiter=limit=0.80:level=false:attack=2:release=40[aout]"
        )
    return ";".join(parts)


def render_master_clip(
    source_slice: Path,
    edit_plan: EditPlan,
    transcript: AnnotatedTranscript,
    out_mp4: Path,
    compute_profile: str = "local_hp",
    use_auto_editor: bool = True,
) -> Path:
    """Render Senior-grade 1080x1920 H.264 clip (60fps, <=1.8x card, jump-cuts, punch-in zoom, bleeper, SFX, loudnorm)."""
    apply_thread_budget()
    cfg: dict[str, Any] = {}
    if CHANNEL_PROFILE_PATH.exists():
        cfg = yaml.safe_load(CHANNEL_PROFILE_PATH.read_text(encoding="utf-8")) or {}
    captions_cfg = cfg.get("captions") or {}
    layout_cfg = cfg.get("layout_9_16") or {}
    clip_cfg = cfg.get("clip_rules") or {}
    auto_cleanup = bool((cfg.get("storage") or {}).get("auto_cleanup_temp", True))
    max_upscale = float(layout_cfg.get("max_webcam_upscale", 1.8))
    excl_pad = int(layout_cfg.get("webcam_exclusion_pad_px", 48))
    punch_zoom = min(1.15, max(1.0, float(layout_cfg.get("rms_punch_in_max_zoom", 1.12))))
    cut_silence_sec = float(clip_cfg.get("cut_silence_over_sec", 0.65))
    synchresis_sec = float(clip_cfg.get("preserve_synchresis_pause_sec", 0.25))

    src_w, src_h, src_fps, _ = probe_video_stream(source_slice)
    src_w, src_h = src_w or 1920, src_h or 1080
    face_norm = detect_face_center_ultraface(source_slice, edit_plan.webcam_box_px) if edit_plan.webcam_box_px else None

    layout = compute_adaptive_layout(
        src_w,
        src_h,
        edit_plan.webcam_box_px,
        face_box_norm=face_norm,
        max_upscale=max_upscale,
        exclusion_pad=excl_pad,
    )
    if not layout["no_webcam_gameplay_overlap"]:
        raise RuntimeError(f"Geometric overlap violation: {layout}")

    edit_plan.layout_mode = layout["layout_mode"]
    edit_plan.gameplay_crop_px = layout["gameplay_crop_px"]
    edit_plan.webcam_upscale_factor = float(layout["webcam_upscale_factor"])
    edit_plan.no_webcam_gameplay_overlap = bool(layout["no_webcam_gameplay_overlap"])

    raw_clip_dur = round(max(1.0, edit_plan.end_trim - edit_plan.start_trim), 3)
    pcm_16k = _extract_clip_pcm_16k(source_slice, edit_plan.start_trim, raw_clip_dur)
    kept_segments: list[tuple[float, float]] | None = None
    ae_info: dict[str, Any] = {"provider": "pcm_fallback", "used": False}
    if use_auto_editor:
        kept_segments, ae_info = detect_segments_auto_editor(
            source_slice=source_slice,
            start_trim=edit_plan.start_trim,
            clip_dur=raw_clip_dur,
            transcript=transcript,
            pcm_16k=pcm_16k,
            margin_sec=synchresis_sec,
            min_cut_sec=max(0.95, cut_silence_sec + 0.30),
        )
    if kept_segments is None:
        kept_segments = compute_jump_cut_segments(
            raw_clip_dur,
            edit_plan.start_trim,
            transcript,
            pcm_16k=pcm_16k,
            cut_silence_over_sec=cut_silence_sec,
            preserve_pause_sec=synchresis_sec,
        )
    out_dur = round(sum(e - s for s, e in kept_segments), 3)

    ff_start = 0.0
    ff_dur = 0.0
    if edit_plan.freeze_frame is not None:
        mapped_ff = remap_time_through_segments(edit_plan.freeze_frame.timestamp_sec, kept_segments)
        ff_start = round(max(1.0, min(out_dur - 1.2, mapped_ff if mapped_ff is not None else out_dur * 0.45)), 3)
        ff_dur = edit_plan.freeze_frame.duration_sec

    punch_windows = find_punch_in_windows(
        transcript,
        edit_plan.start_trim,
        out_dur,
        kept_segments,
        freeze_ts=ff_start if ff_dur > 0 else None,
    )
    bleep_spans = find_opening_profanity_spans(transcript.words, edit_plan.start_trim, kept_segments, shield_sec=7.5)

    work_dir = out_mp4.parent / f".work_{edit_plan.clip_id}"
    work_dir.mkdir(parents=True, exist_ok=True)
    out_mp4.parent.mkdir(parents=True, exist_ok=True)

    try:
        fonts_work = work_dir / "fonts"
        fonts_work.mkdir(parents=True, exist_ok=True)
        if BUNDLED_FONT_PATH.exists():
            shutil.copy2(BUNDLED_FONT_PATH, fonts_work / BUNDLED_FONT_PATH.name)

        generate_karaoke_ass(
            transcript,
            edit_plan,
            work_dir / "subs.ass",
            captions_cfg=captions_cfg,
            kept_segments=kept_segments,
        )
        callout_png = render_freeze_callout(source_slice, edit_plan, work_dir / "callout.png", layout=layout)
        has_callout = callout_png is not None and callout_png.exists()

        loud_stats = _measure_loudnorm_pass1(source_slice, edit_plan.start_trim, raw_clip_dur)
        print(
            f"[RENDERER] layout={layout['layout_mode']} cam_upscale={layout['webcam_upscale_factor']}x "
            f"no_overlap={layout['no_webcam_gameplay_overlap']} src_fps={src_fps} "
            f"dead_air_engine={ae_info.get('provider') if ae_info.get('used') else 'pcm_fallback'} "
            f"jump_segs={len(kept_segments)} punch_ins={len(punch_windows)} bleeps={len(bleep_spans)} "
            f"pass1_i={loud_stats['input_i']} tp={loud_stats['input_tp']}"
        )

        base_cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "info",
            "-ss",
            f"{edit_plan.start_trim:.3f}",
            "-t",
            f"{raw_clip_dur:.3f}",
            "-i",
            str(source_slice.resolve()),
        ]
        next_idx = 1
        callout_idx, mask_idx, frame_idx = -1, -1, -1
        if has_callout:
            base_cmd.extend(["-loop", "1", "-i", "callout.png"])
            callout_idx = next_idx
            next_idx += 1
        if layout["is_framed_card"]:
            cw_card, ch_card = layout["webcam_card_size"]
            _create_webcam_card_assets(cw_card, ch_card, work_dir)
            base_cmd.extend(["-loop", "1", "-i", "cam_mask.png"])
            mask_idx = next_idx
            next_idx += 1
            base_cmd.extend(["-loop", "1", "-i", "cam_frame.png"])
            frame_idx = next_idx
            next_idx += 1

        # Optional subtle CC0 SFX (hook pop at t=0.04s + whoosh on freeze-frame entry)
        sfx_inputs: list[tuple[int, int, float]] = []
        pop_wav = SFX_DIR / "pop.wav"
        if pop_wav.exists():
            shutil.copy2(pop_wav, work_dir / "pop.wav")
            base_cmd.extend(["-i", "pop.wav"])
            sfx_inputs.append((next_idx, 40, 0.09))
            next_idx += 1
        whoosh_wav = SFX_DIR / "whoosh.wav"
        if has_callout and whoosh_wav.exists() and ff_start >= 0.25:
            shutil.copy2(whoosh_wav, work_dir / "whoosh.wav")
            base_cmd.extend(["-i", "whoosh.wav"])
            sfx_inputs.append((next_idx, max(0, round((ff_start - 0.14) * 1000)), 0.12))
            next_idx += 1

        flt = _build_adaptive_filter(
            layout=layout,
            has_callout=has_callout,
            ff_start=ff_start,
            ff_dur=ff_dur,
            loud_stats=loud_stats,
            callout_idx=callout_idx,
            mask_idx=mask_idx,
            frame_idx=frame_idx,
            kept_segments=kept_segments,
            punch_windows=punch_windows,
            punch_zoom=punch_zoom,
            bleep_spans=bleep_spans,
            sfx_inputs=sfx_inputs,
            out_dur=out_dur,
        )

        base_cmd.extend([
            "-sws_flags",
            "lanczos",
            "-filter_complex",
            flt,
            "-map",
            "[vout]",
            "-map",
            "[aout]",
            "-t",
            f"{out_dur:.3f}",
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

        primary_enc = (
            ["-c:v", "h264_nvenc", "-cq", "19"]
            if compute_profile in {"gcp_spot_l4", "colab_t4"}
            else ["-c:v", "h264_qsv", "-global_quality", "18", "-look_ahead", "0"]
        )
        res = subprocess.run(
            base_cmd + primary_enc + [str(out_mp4.resolve())],
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        stderr_used = res.stderr or ""
        if res.returncode != 0:
            print(f"[RENDERER] primary encoder fallback -> libx264 ({stderr_used[-200:]})")
            fb = subprocess.run(
                base_cmd
                + [
                    "-c:v",
                    "libx264",
                    "-preset",
                    "fast",
                    "-crf",
                    "18",
                    "-maxrate",
                    "14M",
                    "-bufsize",
                    "20M",
                    "-pix_fmt",
                    "yuv420p",
                    str(out_mp4.resolve()),
                ],
                cwd=str(work_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            stderr_used = fb.stderr or ""
            if fb.returncode != 0:
                raise RuntimeError(f"FFmpeg render failed: {stderr_used[-300:]}")

        fonts_loaded = sorted(set(_FONTSELECT_RE.findall(stderr_used)))
        if fonts_loaded:
            print(f"[RENDERER] libass fontselect={fonts_loaded[:4]}")
    finally:
        if auto_cleanup:
            shutil.rmtree(work_dir, ignore_errors=True)
        enforce_runs_quota(RUNS_DIR, protect_dir=out_mp4.parent)

    return out_mp4
