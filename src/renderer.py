"""Senior Studio 9:16 renderer (v3.6-A): 60fps CFR, pre-amix voice clarity, riser + sidechain ducking, and `ffmpeg-normalize` EBU R128."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import wave
from collections.abc import Sequence
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np
from ffmpeg_normalize import FFmpegNormalize
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from src.callout import OVERLAY_DEFAULT_Y, render_editorial_callout
from src.captions import generate_karaoke_ass, remap_time_through_segments
from src.miner import (
    CHANNEL_PROFILE_PATH,
    RUNS_DIR,
    enforce_runs_quota,
    probe_video_stream,
)
from src.resource_guard import apply_thread_budget
from src.runtime_paths import (
    BUNDLED_FONT_PATH,
    PROJECT_ROOT,
    ensure_media_path,
    resolve_binary,
    resolve_binary_str,
)
from src.schemas import AnnotatedTranscript, EditPlan, SceneBlueprint, WordTimestamp
from src.speaker_guard import detect_face_center_ultraface, score_streamer_speech_asd

_FONTSELECT_RE = re.compile(r"fontselect:\s*\(([^)]+)\)", re.IGNORECASE)
_PROFANITY_RE = re.compile(r"\b(fuck\w*|shit\w*|bitch\w*|cunt\w*|nigg\w*|whore\w*|slut\w*|motherfuck\w*)\b", re.IGNORECASE)
SFX_DIR = PROJECT_ROOT / "assets" / "sfx"


def _voice_clarity_chain(highpass_hz: int = 70) -> str:
    hz = max(40, min(160, int(highpass_hz)))
    return f"highpass=f={hz},acompressor=threshold=-20dB:ratio=2.2:attack=12:release=120:makeup=1.5"


def _even(n: int) -> int:
    return max(2, int(n) & ~1)


def rects_overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    """Return True if [x1, y1, x2, y2] rectangles have positive intersection area."""
    return not (a[2] <= b[0] or a[0] >= b[2] or a[3] <= b[1] or a[1] >= b[3])


def compute_webcam_card_pos(card_w: int, card_h: int) -> tuple[int, int]:
    """Anchor rounded webcam card cleanly below TikTok/Reels y=180 top bar and centered inside top 692px band."""
    px = (1080 - card_w) // 2
    max_y = max(0, 692 - card_h - 10)
    ideal_y = max(180, 692 - card_h - 16)
    py = min(max_y, ideal_y)
    return px, py


def map_output_ts_to_source_offset(out_ts: float, kept_segments: list[tuple[float, float]]) -> float:
    """Inverse of `remap_time_through_segments`: map output clip timestamp `out_ts` back to `source_slice` relative offset."""
    if not kept_segments:
        return max(0.0, float(out_ts))
    target = max(0.0, float(out_ts))
    acc = 0.0
    for seg_s, seg_e in kept_segments:
        seg_len = max(0.0, seg_e - seg_s)
        if target <= acc + seg_len + 1e-6:
            return round(min(seg_e, seg_s + max(0.0, target - acc)), 3)
        acc += seg_len
    return round(kept_segments[-1][1], 3)


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
                resolve_binary_str("ffmpeg"),
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
            timeout=60,
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

    total_kept = sum(e - s for s, e in kept)
    if total_kept < 15.0 or len(kept) <= 1:
        return [(0.0, round(clip_dur, 3))]
    return kept


def find_auto_editor_executable() -> Path | None:
    """Locate auto-editor CLI cross-platform via `src.runtime_paths`."""
    return resolve_binary("auto-editor")


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
    """Run WyattBlue/auto-editor (`--export v3`) for dead-air detection with gaming Synchresis guardrails."""
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
                resolve_binary_str("ffmpeg"),
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


def normalize_audio_ebu_r128(
    in_mp4: Path,
    out_mp4: Path,
    *,
    target_lufs: float = -14.0,
    true_peak: float = -1.2,
    sample_rate: int = 48000,
) -> dict[str, Any]:
    """Master post-mix / post-jumpcut clip audio to exact EBU R128 (`-14.0 LUFS`) via `ffmpeg-normalize` (`-c:v copy`)."""
    ensure_media_path()
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    eff_tp = min(-1.8, float(true_peak))
    try:
        normalizer = FFmpegNormalize(
            normalization_type="ebu",
            target_level=float(target_lufs),
            true_peak=eff_tp,
            loudness_range_target=11.0,
            keep_lra_above_loudness_range_target=True,
            audio_codec="aac",
            audio_bitrate=256000,
            sample_rate=int(sample_rate),
            audio_channels=2,
            post_filter="alimiter=limit=0.78:level=false:attack=1.5:release=25",
            video_codec="copy",
            extra_output_options=["-movflags", "+faststart"],
            progress=False,
        )
        normalizer.add_media_file(str(in_mp4.resolve()), str(out_mp4.resolve()))
        normalizer.run_normalization()
        if out_mp4.is_file() and out_mp4.stat().st_size > 0:
            return {"engine": "ffmpeg-normalize", "normalized": True, "target_lufs": target_lufs, "true_peak": eff_tp}
    except Exception as exc:
        # Pure silent test inputs (-inf LUFS) cannot be scaled linearly; preserve video+audio stream cleanly
        shutil.copy2(in_mp4, out_mp4)
        return {"engine": "ffmpeg-normalize", "normalized": False, "fallback_reason": str(exc)[:180]}

    shutil.copy2(in_mp4, out_mp4)
    return {"engine": "ffmpeg-normalize", "normalized": False, "fallback_reason": "empty_output"}


def render_ab_hook_variant(
    master_mp4: Path,
    alt_hook_text: str,
    out_mp4: Path,
    duration_sec: float = 3.0,
    compute_profile: str = "local_hp",
) -> Path:
    """Render Variant B MP4 (0-3s alternative hook banner + fresh CFR re-encode hash) for IG Trial Reels / TikTok A/B."""
    apply_thread_budget()
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    clean_hook = " ".join(alt_hook_text.strip().upper().split()[:6]) or "WAIT FOR THIS"
    _, _, src_fps, _ = probe_video_stream(master_mp4)
    fps_int = 60 if (src_fps <= 0 or src_fps >= 50.0) else 30

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
        from src.schemas import UNIVERSAL_SAFE_BOX_9_16

        safe_x0, _, safe_x1, _ = UNIVERSAL_SAFE_BOX_9_16
        safe_w = safe_x1 - safe_x0
        pad_x, pad_y = 30, 18
        box_w = min(safe_w, tw + 2 * pad_x)
        box_h = th + 2 * pad_y
        bx1 = safe_x0 + max(0, (safe_w - box_w) // 2)
        by1 = 682
        bx2, by2 = bx1 + box_w, by1 + box_h
        draw.rounded_rectangle([bx1, by1, bx2, by2], radius=18, fill=(10, 12, 18, 224), outline=(255, 212, 0, 235), width=4)
        tx = bx1 + max(8, (box_w - tw) // 2)
        ty = by1 + pad_y - bbox[1]
        draw.text((tx, ty), clean_hook, font=font, fill=(255, 224, 64, 255), stroke_width=3, stroke_fill=(6, 8, 14, 255))
        ov.save(banner_png, format="PNG")

        start_s = 0.05
        end_s = max(0.8, min(4.0, duration_sec))
        flt = (
            f"[1:v]trim=duration={end_s:.3f},setpts=PTS-STARTPTS+{start_s:.3f}/TB[ov];"
            f"[0:v][ov]overlay=0:0:enable='between(t,{start_s:.3f},{start_s + end_s:.3f})':eof_action=pass,fps={fps_int},format=nv12[vout]"
        )
        base_cmd = [
            resolve_binary_str("ffmpeg"),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(master_mp4.resolve()),
            "-framerate",
            str(fps_int),
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
            "-fps_mode",
            "cfr",
            "-r",
            str(fps_int),
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
        ]
        enc = (
            ["-c:v", "h264_nvenc", "-cq", "19"]
            if compute_profile in {"gcp_spot_l4", "colab_t4"}
            else ["-c:v", "h264_qsv", "-global_quality", "18", "-look_ahead", "0"]
        )
        res = subprocess.run(
            base_cmd + enc + [str(out_mp4.resolve())],
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
        if res.returncode != 0 or not out_mp4.exists():
            fb = subprocess.run(
                base_cmd + ["-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p", str(out_mp4.resolve())],
                cwd=str(work_dir),
                capture_output=True,
                text=True,
                check=False,
                timeout=300,
            )
            if fb.returncode != 0:
                raise RuntimeError(f"A/B variant render failed: {fb.stderr[-300:]}")
    return out_mp4


def detect_scene_cuts_pyscenedetect(
    source_slice: Path,
    start_trim: float,
    clip_dur: float,
    threshold: float = 27.0,
) -> list[float]:
    """Detect hard scene cuts inside [start_trim .. start_trim+clip_dur] using PySceneDetect (BSD-3-Clause)."""
    if not source_slice.is_file() or clip_dur <= 0.5:
        return []
    try:
        from scenedetect import ContentDetector, SceneManager, open_video

        video = open_video(str(source_slice.resolve()))
        if start_trim > 0.05:
            video.seek(start_trim)
        sm = SceneManager()
        sm.add_detector(ContentDetector(threshold=threshold))
        sm.detect_scenes(video=video, duration=clip_dur, frame_skip=1)
        scene_list = sm.get_scene_list()
        cuts: list[float] = []
        for idx, (s_tc, _e_tc) in enumerate(scene_list):
            if idx == 0:
                continue
            rel = round(float(s_tc.get_seconds()) - start_trim, 3)
            if 0.15 < rel < clip_dur - 0.15:
                cuts.append(rel)
        return sorted(set(cuts))
    except Exception:
        return []


def guard_windows_against_scene_cuts(
    windows: list[tuple[float, float]],
    scene_cuts: list[float] | None,
    min_dur: float = 0.65,
    margin_sec: float = 0.08,
) -> list[tuple[float, float]]:
    """Senior Rule (a): NEVER allow a pan or punch-in zoom window to cross a hard scene cut."""
    if not windows or not scene_cuts:
        return list(windows)

    cuts = sorted(float(c) for c in scene_cuts)
    safe_windows: list[tuple[float, float]] = []
    for ws, we in windows:
        crossing = [c for c in cuts if (ws - margin_sec) < c < (we + margin_sec)]
        if not crossing:
            if we - ws >= min_dur:
                safe_windows.append((round(ws, 3), round(we, 3)))
            continue

        segments: list[tuple[float, float]] = []
        cursor = ws
        for c in crossing:
            seg_end = min(we, c - margin_sec)
            if seg_end - cursor >= min_dur:
                segments.append((round(cursor, 3), round(seg_end, 3)))
            cursor = max(cursor, c + margin_sec)
        if we - cursor >= min_dur:
            segments.append((round(cursor, 3), round(we, 3)))
        if segments:
            best_seg = max(segments, key=lambda s: s[1] - s[0])
            safe_windows.append(best_seg)
    return safe_windows


def trim_post_climax_dead_tail(
    kept_segments: list[tuple[float, float]],
    pcm_16k: np.ndarray | None,
    transcript: AnnotatedTranscript,
    start_trim: float,
    max_tail_sec: float = 0.40,
    min_clip_sec: float = 15.0,
    silence_db_thresh: float = -38.0,
) -> list[tuple[float, float]]:
    """Trim dead trailing acoustic silence (> max_tail_sec below -38 dBFS) at the end of the clip for crisp loops."""
    if not kept_segments or pcm_16k is None or len(pcm_16k) < 3200:
        return kept_segments

    last_s, last_e = kept_segments[-1]
    total_dur = sum(e - s for s, e in kept_segments)
    if total_dur <= min_clip_sec + 0.3 or last_e - last_s <= 1.2:
        return kept_segments

    speech_end_rel = 0.0
    for w in transcript.words:
        if not w.is_npc_or_other and w.end > start_trim:
            rel_e = w.end - start_trim
            if rel_e <= last_e:
                speech_end_rel = max(speech_end_rel, rel_e)
    for ev in transcript.non_verbal_events:
        if ev.kind in {"shouting", "laughter", "visual_motion"} and ev.end > start_trim:
            rel_e = ev.end - start_trim
            if rel_e <= last_e:
                speech_end_rel = max(speech_end_rel, rel_e)

    sr = 16000
    step_s = 0.10
    active_end_rel = speech_end_rel
    t_cur = min(last_e, len(pcm_16k) / float(sr))
    while t_cur > max(last_s + 0.5, speech_end_rel):
        s_idx = max(0, int((t_cur - step_s) * sr))
        e_idx = min(len(pcm_16k), int(t_cur * sr))
        if e_idx <= s_idx:
            break
        seg = pcm_16k[s_idx:e_idx]
        rms = float(np.sqrt(np.mean(seg * seg)) + 1e-9)
        db = 20.0 * float(np.log10(rms))
        if db > silence_db_thresh:
            active_end_rel = max(active_end_rel, t_cur)
            break
        t_cur -= step_s

    target_end = min(last_e, active_end_rel + max_tail_sec)
    if last_e - target_end <= 0.15:
        return kept_segments

    min_allowed_end = max(last_s + 1.0, last_e - (total_dur - min_clip_sec))
    new_last_e = round(max(min_allowed_end, target_end), 3)
    if new_last_e >= last_e - 0.08:
        return kept_segments
    return [*kept_segments[:-1], (last_s, new_last_e)]


def find_punch_in_windows(
    transcript: AnnotatedTranscript,
    start_trim: float,
    out_dur: float,
    kept_segments: list[tuple[float, float]] | None,
    freeze_ts: float | None = None,
    max_windows: int = 2,
    scene_cuts: list[float] | None = None,
) -> list[tuple[float, float]]:
    """Select up to 2 short motivated 1.10x-1.15x punch-in zoom windows guarded strictly against PySceneDetect hard cuts."""
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
        win_s = round(max(1.0, freeze_ts - 1.25), 3)
        win_e = round(min(out_dur - 0.4, freeze_ts + 0.35), 3)
        cands.append((0.95, win_s, win_e))

    cands.sort(key=lambda x: x[0], reverse=True)
    chosen: list[tuple[float, float]] = []
    for _, ws, we in cands:
        guarded = guard_windows_against_scene_cuts([(ws, we)], scene_cuts, min_dur=0.65, margin_sec=0.08)
        if not guarded:
            continue
        gws, gwe = guarded[0]
        if all(gwe + 3.0 < cs or gws > ce + 3.0 for cs, ce in chosen):
            chosen.append((gws, gwe))
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
        fy1_px = cam_y + round(fy1 * cam_h)
        fh_px = max(32, round(max(0.18, fy2 - fy1) * cam_h))
        sub_w = _even(max(64, round(cam_w * 0.96)))
        target_h = max(round(fh_px * 1.18), round(sub_w / 1.72))
        sub_h = _even(min(cam_h, max(64, target_h)))
        headroom = max(12, round(fh_px * 0.16))
        desired_y = fy1_px - headroom
        cam_x = max(wx + inset, min(wx + ww - inset - sub_w, fcx - sub_w // 2)) & ~1
        cam_y = max(wy + inset, min(wy + wh - inset - sub_h, desired_y)) & ~1
        cam_w, cam_h = sub_w, sub_h

    if cam_w >= 540:
        card_w, card_h = 1080, 692
        upscale = round(1080.0 / float(cam_w), 2)
        is_framed_card = False
    else:
        scale = min(max_upscale, 920.0 / float(cam_w), 496.0 / float(cam_h))
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
    ideal_x = _even((src_w - ideal_gw) // 2)
    cam_center_x = wx + ww * 0.5
    screen_cx = src_w // 2

    if cam_center_x < src_w * 0.5:
        avail_w = src_w - forb[2]
        if ideal_x >= forb[2] and ideal_x + ideal_gw <= src_w:
            gw, gh, gx, gy = ideal_gw, _even(src_h), ideal_x, 0
        else:
            sym_half = screen_cx - forb[2]
            sym_gw = _even(max(0, sym_half * 2))
            if sym_gw >= 560:
                gw = min(ideal_gw, sym_gw)
                gh = _even(min(src_h, round(gw / target_ar)))
                gx = _even(max(forb[2], screen_cx - gw // 2))
                gy = _even((src_h - gh) // 2)
            elif avail_w >= ideal_gw:
                gw, gh = ideal_gw, _even(src_h)
                gx = _even(max(forb[2], min(src_w - gw, ideal_x)))
                gy = 0
            else:
                gw = _even(max(200, avail_w))
                gh = _even(min(src_h, round(gw / target_ar)))
                gx = _even(forb[2])
                gy = _even((src_h - gh) // 2)
    else:
        avail_w = forb[0]
        if ideal_x >= 0 and ideal_x + ideal_gw <= forb[0]:
            gw, gh, gx, gy = ideal_gw, _even(src_h), ideal_x, 0
        else:
            sym_half = forb[0] - screen_cx
            sym_gw = _even(max(0, sym_half * 2))
            if sym_gw >= 560:
                gw = min(ideal_gw, sym_gw)
                gh = _even(min(src_h, round(gw / target_ar)))
                gx = _even(max(0, min(forb[0] - gw, screen_cx - gw // 2)))
                gy = _even((src_h - gh) // 2)
            elif avail_w >= ideal_gw:
                gw, gh = ideal_gw, _even(src_h)
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
    """Generate rounded alpha mask (card_w x card_h) and soft drop-shadow + radial studio vignette frame (1080 x 692)."""
    radius = 24
    mask = Image.new("L", (card_w, card_h), 0)
    mdraw = ImageDraw.Draw(mask)
    mdraw.rounded_rectangle([0, 0, card_w - 1, card_h - 1], radius=radius, fill=255)
    mask_path = work_dir / "cam_mask.png"
    mask.save(mask_path, format="PNG")

    px, py = compute_webcam_card_pos(card_w, card_h)
    yy, xx = np.mgrid[0:692, 0:1080]
    dist = np.sqrt(((xx - 540.0) / 680.0) ** 2 + ((yy - 410.0) / 460.0) ** 2)
    vig_a = np.clip((dist - 0.45) * 110.0, 0.0, 90.0).astype(np.uint8)
    vig_arr = np.zeros((692, 1080, 4), dtype=np.uint8)
    vig_arr[..., 0] = 6
    vig_arr[..., 1] = 8
    vig_arr[..., 2] = 14
    vig_arr[..., 3] = vig_a
    vignette_img = Image.fromarray(vig_arr, mode="RGBA")

    shadow = Image.new("RGBA", (1080, 692), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shadow)
    sdraw.rounded_rectangle([px - 5, py + 8, px + card_w + 5, py + card_h + 15], radius=radius + 4, fill=(0, 0, 0, 185))
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=18))
    combined = Image.alpha_composite(vignette_img, shadow)

    cut = Image.new("L", (1080, 692), 255)
    ImageDraw.Draw(cut).rounded_rectangle([px, py, px + card_w - 1, py + card_h - 1], radius=radius, fill=0)
    r, g, b, a = combined.split()
    a_masked = Image.composite(a, Image.new("L", (1080, 692), 0), cut)
    frame = Image.merge("RGBA", (r, g, b, a_masked))
    fdraw = ImageDraw.Draw(frame)
    fdraw.rounded_rectangle([px, py, px + card_w - 1, py + card_h - 1], radius=radius, outline=(245, 248, 255, 175), width=4)
    fdraw.rectangle([0, 688, 1080, 692], fill=(10, 12, 16, 235))
    frame_path = work_dir / "cam_frame.png"
    frame.save(frame_path, format="PNG")
    return mask_path, frame_path


def _build_adaptive_filter(
    layout: dict[str, Any],
    has_callout: bool,
    ff_start: float,
    ff_dur: float,
    callout_idx: int,
    mask_idx: int,
    frame_idx: int,
    kept_segments: list[tuple[float, float]],
    punch_windows: list[tuple[float, float]],
    punch_zoom: float,
    bleep_spans: list[tuple[float, float]],
    sfx_inputs: Sequence[tuple[int, int, float] | tuple[int, int, float, float | None]],
    out_dur: float,
    target_fps: int = 60,
    highpass_hz: int = 70,
    sample_rate: int = 48000,
    callout_mode: str = "motion_overlay",
    callout_y: int = OVERLAY_DEFAULT_Y,
    loud_stats: dict[str, Any] | None = None,
) -> str:
    _ = loud_stats
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

    # Step 2: Adaptive 9:16 video layout + optional motivated cut-guarded punch-in zoom
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
        n_splits = 2 + (1 if need_bot_zoom else 0)
        split_labels = [f"[vs{i}]" for i in range(n_splits)]
        parts.append(f"[vsrc]split={n_splits}{''.join(split_labels)}")

        s_ptr = 0
        if layout["is_framed_card"]:
            px, py = compute_webcam_card_pos(card_w, card_h)
            parts.append(f"{split_labels[s_ptr]}crop={cw}:{ch}:{cx}:{cy},split=2[cam_fg_src][cam_bg_src]")
            s_ptr += 1
            parts.append(
                "[cam_bg_src]scale=270:173:force_original_aspect_ratio=increase:flags=bilinear,"
                "crop=270:173,boxblur=14:3,eq=brightness=-0.16:saturation=0.72:contrast=0.92,"
                "scale=1080:692:flags=bilinear,format=yuv420p,setsar=1[top_bg]"
            )
            parts.append(
                f"[cam_fg_src]scale={card_w}:{card_h}:flags=lanczos,unsharp=3:3:0.35,format=rgba,setsar=1[cam_raw]"
            )
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

    # Step 3: Motion-graphics alpha overlay over LIVE video (default) OR explicit opt-in comedic_freeze
    if has_callout:
        ff_end = ff_start + ff_dur
        if callout_mode == "comedic_freeze":
            parts.append(
                f"[{callout_idx}:v]trim=duration={ff_dur:.3f},setpts=PTS-STARTPTS+{ff_start:.3f}/TB[ff];"
                f"[base][ff]overlay=0:0:enable='between(t,{ff_start:.3f},{ff_end:.3f})':eof_action=pass[ov]"
            )
        else:
            parts.append(
                f"[{callout_idx}:v]format=rgba,setpts=PTS-STARTPTS+{ff_start:.3f}/TB[ov_mg];"
                f"[base][ov_mg]overlay=0:{callout_y}:enable='between(t,{ff_start:.3f},{ff_end:.3f})':eof_action=pass[ov]"
            )
    else:
        parts.append("[base]null[ov]")

    parts.append(f"[ov]ass=subs.ass:fontsdir=fonts,fps={target_fps},format=nv12[vout]")

    # Step 4: Audio chain — apply `_voice_clarity_chain` BEFORE mixing SFX, sidechain-duck SFX/riser under voice,
    # mix optional 0-7.5s bleep, and finish with 30ms seamless-loop out-fade (mastered post-render by ffmpeg-normalize).
    clarity = _voice_clarity_chain(highpass_hz)
    sr = int(sample_rate)
    if bleep_spans:
        en_bleep = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in bleep_spans)
        parts.append(
            f"[asrc]aresample={sr},aformat=channel_layouts=stereo,volume=0:enable='{en_bleep}',{clarity}[avoice_clean]"
        )
        parts.append(
            f"aevalsrc=0.14*sin(2*PI*1000*t):s={sr}:c=stereo:d={out_dur:.3f},"
            f"volume=0:enable='not({en_bleep})'[a_bleep]"
        )
    else:
        parts.append(f"[asrc]aresample={sr},aformat=channel_layouts=stereo,{clarity}[avoice_clean]")

    fade_start = max(0.1, round(out_dur - 0.030, 3))
    if sfx_inputs:
        parts.append("[avoice_clean]asplit=2[avoice_main][avoice_sc]")
        parts.append(f"anullsrc=r={sr}:cl=stereo:d={out_dur:.3f}[sfx_zero]")
        sfx_lbls = ["[sfx_zero]"]
        for sfx_i, sfx_spec in enumerate(sfx_inputs):
            if len(sfx_spec) == 4:
                inp_idx, delay_ms, gain, trim_dur = sfx_spec
            else:
                inp_idx, delay_ms, gain = sfx_spec
                trim_dur = None
            lbl = f"[sfx{sfx_i}]"
            trim_flt = f"atrim=duration={trim_dur:.3f},afade=t=in:st=0:d=0.12," if trim_dur is not None else ""
            parts.append(
                f"[{inp_idx}:a]aresample={sr},aformat=channel_layouts=stereo,"
                f"{trim_flt}volume={gain:.3f},adelay={delay_ms}|{delay_ms}{lbl}"
            )
            sfx_lbls.append(lbl)

        parts.append(
            f"{''.join(sfx_lbls)}amix=inputs={len(sfx_lbls)}:duration=first:dropout_transition=0:normalize=0[sfx_bed]"
        )
        parts.append(
            "[sfx_bed][avoice_sc]sidechaincompress=threshold=0.04:ratio=4:attack=8:release=180[sfx_ducked]"
        )
        final_inputs = ["[avoice_main]", "[sfx_ducked]"] + (["[a_bleep]"] if bleep_spans else [])
        parts.append(
            f"{''.join(final_inputs)}amix=inputs={len(final_inputs)}:duration=first:dropout_transition=0:normalize=0,"
            f"alimiter=limit=0.86:level=false:attack=2:release=40,"
            f"afade=t=out:st={fade_start:.3f}:d=0.030[aout]"
        )
    elif bleep_spans:
        parts.append(
            "[avoice_clean][a_bleep]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
            "alimiter=limit=0.86:level=false:attack=2:release=40,"
            f"afade=t=out:st={fade_start:.3f}:d=0.030[aout]"
        )
    else:
        parts.append(
            "[avoice_clean]alimiter=limit=0.86:level=false:attack=2:release=40,"
            f"afade=t=out:st={fade_start:.3f}:d=0.030[aout]"
        )

    return ";".join(parts)


def render_master_clip(
    source_slice: Path,
    edit_plan: EditPlan,
    transcript: AnnotatedTranscript,
    out_mp4: Path,
    compute_profile: str = "local_hp",
    use_auto_editor: bool = True,
    clean_cover_out: Path | None = None,
    enforce_speaker_guard: bool = True,
    scene_blueprint: SceneBlueprint | None = None,
    cover_rank_offset: int = 0,
    callout_y_override: int | None = None,
) -> Path:
    """Render Senior-grade v3.6-A 1080x1920 60fps CFR clip + pre-amix voice clarity + riser/sidechain + `ffmpeg-normalize`."""
    apply_thread_budget()
    ensure_media_path()
    from src.niche_packs import load_niche_pack, silence_policy_cut_threshold_sec
    from src.settings import load_channel_profile

    chan_prof = load_channel_profile(CHANNEL_PROFILE_PATH)
    captions_cfg = chan_prof.captions.model_dump()
    layout_cfg = chan_prof.layout_9_16.model_dump()
    clip_cfg = chan_prof.clip_rules.model_dump()
    audio_cfg = chan_prof.audio.model_dump()
    auto_cleanup = bool(chan_prof.storage.auto_cleanup_temp)

    max_upscale = float(layout_cfg.get("max_webcam_upscale", 1.8))
    excl_pad = int(layout_cfg.get("webcam_exclusion_pad_px", 48))
    punch_zoom = min(1.15, max(1.10, float(layout_cfg.get("rms_punch_in_max_zoom", 1.12))))
    if scene_blueprint is not None and "zoom" in scene_blueprint.overlay_and_effects_guide.lower():
        punch_zoom = min(1.15, max(punch_zoom, 1.13))
    base_silence_sec = float(clip_cfg.get("cut_silence_over_sec", 0.65))
    try:
        active_pack = load_niche_pack(chan_prof.studio.default_niche)
        cut_silence_sec = silence_policy_cut_threshold_sec(active_pack, default_sec=base_silence_sec)
    except Exception:
        cut_silence_sec = base_silence_sec
    synchresis_sec = float(clip_cfg.get("preserve_synchresis_pause_sec", 0.25))

    target_lufs = float(audio_cfg.get("target_lufs", -14.0))
    target_tp = float(audio_cfg.get("true_peak_dbtp", -1.0))
    highpass_hz = int(audio_cfg.get("highpass_hz", 70))
    sample_rate = int(audio_cfg.get("sample_rate", 48000))

    if (
        enforce_speaker_guard
        and edit_plan.webcam_box_px is not None
        and transcript.words
        and all(w.streamer_asd_score is None for w in transcript.words)
    ):
        asd_info = score_streamer_speech_asd(source_slice, edit_plan.webcam_box_px, transcript.words)
        print(f"[RENDERER_LR_ASD] {edit_plan.clip_id}: {asd_info}")

    src_w, src_h, src_fps, _ = probe_video_stream(source_slice)
    src_w, src_h = src_w or 1920, src_h or 1080
    cfg_fps = int(layout_cfg.get("fps", 60) or 60)
    target_fps = cfg_fps if (src_fps <= 0 or abs(src_fps - cfg_fps) <= 5.0 or not layout_cfg.get("preserve_source_fps", True)) else (60 if src_fps >= 48.0 else 30)

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
    kept_segments = trim_post_climax_dead_tail(
        kept_segments=kept_segments,
        pcm_16k=pcm_16k,
        transcript=transcript,
        start_trim=edit_plan.start_trim,
        max_tail_sec=0.40,
        min_clip_sec=15.0,
    )
    out_dur = round(sum(e - s for s, e in kept_segments), 3)

    raw_scene_cuts = detect_scene_cuts_pyscenedetect(source_slice, edit_plan.start_trim, raw_clip_dur)
    mapped_scene_cuts = sorted(
        {
            mc
            for c in raw_scene_cuts
            if (mc := remap_time_through_segments(c, kept_segments)) is not None and 0.1 < mc < out_dur - 0.1
        }
    )

    ff_start = 0.0
    ff_dur = 0.0
    callout_mode = "motion_overlay"
    callout_y = OVERLAY_DEFAULT_Y
    callout_meta: dict[str, Any] | None = None
    if edit_plan.freeze_frame is not None:
        mapped_ff = remap_time_through_segments(edit_plan.freeze_frame.timestamp_sec, kept_segments)
        callout_mode = edit_plan.freeze_frame.mode
        base_dur = (
            min(1.0, max(0.5, float(edit_plan.freeze_frame.duration_sec)))
            if callout_mode == "comedic_freeze"
            else max(1.5, min(2.5, float(edit_plan.freeze_frame.duration_sec if edit_plan.freeze_frame.duration_sec >= 1.2 else 1.8)))
        )
        ff_dur = round(min(base_dur, max(0.8, out_dur - 2.0)), 3)
        ff_start = round(max(1.0, min(out_dur - ff_dur - 0.4, mapped_ff if mapped_ff is not None else out_dur * 0.45)), 3)

    punch_windows = find_punch_in_windows(
        transcript,
        edit_plan.start_trim,
        out_dur,
        kept_segments,
        freeze_ts=ff_start if ff_dur > 0 else None,
        scene_cuts=mapped_scene_cuts,
    )
    bleep_spans = find_opening_profanity_spans(transcript.words, edit_plan.start_trim, kept_segments, shield_sec=7.5)

    work_dir = out_mp4.parent / f".work_{edit_plan.clip_id}"
    work_dir.mkdir(parents=True, exist_ok=True)
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg_exe = resolve_binary_str("ffmpeg")

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
        callout_meta = render_editorial_callout(source_slice, edit_plan, work_dir, layout=layout)
        has_callout = callout_meta is not None and Path(callout_meta["path"]).is_file()
        if callout_meta is not None:
            callout_mode = str(callout_meta["mode"])
            callout_y = int(callout_y_override if callout_y_override is not None else callout_meta["overlay_y"])
            ff_dur = float(callout_meta["duration_sec"])

        base_cmd = [
            ffmpeg_exe,
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
        if has_callout and callout_meta is not None:
            cpath = Path(callout_meta["path"])
            if callout_mode == "comedic_freeze":
                base_cmd.extend(["-framerate", str(target_fps), "-loop", "1", "-i", cpath.name])
            else:
                base_cmd.extend(["-i", cpath.name])
            callout_idx = next_idx
            next_idx += 1
        if layout["is_framed_card"]:
            cw_card, ch_card = layout["webcam_card_size"]
            _create_webcam_card_assets(cw_card, ch_card, work_dir)
            base_cmd.extend(["-framerate", str(target_fps), "-loop", "1", "-i", "cam_mask.png"])
            mask_idx = next_idx
            next_idx += 1
            base_cmd.extend(["-framerate", str(target_fps), "-loop", "1", "-i", "cam_frame.png"])
            frame_idx = next_idx
            next_idx += 1

        # Calibrated CC0 SFX (pop + pre-climax riser + callout whoosh/hit), all sidechain-ducked under voice
        sfx_inputs: list[tuple[int, int, float, float | None]] = []
        pop_wav = SFX_DIR / "pop.wav"
        if pop_wav.exists():
            shutil.copy2(pop_wav, work_dir / "pop.wav")
            base_cmd.extend(["-i", "pop.wav"])
            sfx_inputs.append((next_idx, 40, 0.13, None))
            next_idx += 1

        riser_wav = SFX_DIR / "riser.wav"
        climax_anchor = ff_start if (has_callout and ff_start >= 1.3) else (punch_windows[0][0] if punch_windows and punch_windows[0][0] >= 1.3 else 0.0)
        if riser_wav.exists() and climax_anchor >= 1.3:
            shutil.copy2(riser_wav, work_dir / "riser.wav")
            riser_dur = round(min(1.25, max(0.65, climax_anchor - 0.15)), 3)
            riser_delay_ms = max(0, round((climax_anchor - riser_dur) * 1000.0))
            base_cmd.extend(["-i", "riser.wav"])
            sfx_inputs.append((next_idx, riser_delay_ms, 0.12, riser_dur))
            next_idx += 1

        if has_callout and callout_meta is not None and ff_start >= 0.15:
            sfx_kind = str(callout_meta.get("sfx") or "whoosh")
            sfx_map = {
                "whoosh": ("whoosh.wav", 0.16),
                "pop": ("pop.wav", 0.15),
                "hit": ("impact_sub.wav", 0.18),
            }
            if sfx_kind in sfx_map:
                fname, gain = sfx_map[sfx_kind]
                src_sfx = SFX_DIR / fname
                if src_sfx.exists():
                    work_sfx = work_dir / f"callout_{fname}"
                    shutil.copy2(src_sfx, work_sfx)
                    lead_frames = int(callout_meta.get("sfx_lead_frames") or 2)
                    lead_sec = lead_frames / max(24.0, float(target_fps))
                    delay_ms = max(0, round((ff_start - lead_sec) * 1000.0))
                    base_cmd.extend(["-i", work_sfx.name])
                    sfx_inputs.append((next_idx, delay_ms, gain, None))
                    next_idx += 1

        flt = _build_adaptive_filter(
            layout=layout,
            has_callout=has_callout,
            ff_start=ff_start,
            ff_dur=ff_dur,
            callout_idx=callout_idx,
            mask_idx=mask_idx,
            frame_idx=frame_idx,
            kept_segments=kept_segments,
            punch_windows=punch_windows,
            punch_zoom=punch_zoom,
            bleep_spans=bleep_spans,
            sfx_inputs=sfx_inputs,
            out_dur=out_dur,
            target_fps=target_fps,
            highpass_hz=highpass_hz,
            sample_rate=sample_rate,
            callout_mode=callout_mode,
            callout_y=callout_y,
        )

        pre_norm_mp4 = work_dir / "pre_norm.mp4"
        base_cmd.extend([
            "-sws_flags",
            "lanczos",
            "-filter_complex",
            flt,
            "-map",
            "[vout]",
            "-map",
            "[aout]",
            "-fps_mode",
            "cfr",
            "-r",
            str(target_fps),
            "-t",
            f"{out_dur:.3f}",
            "-c:a",
            "pcm_s16le",
            "-ar",
            str(sample_rate),
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
            base_cmd + primary_enc + [str(pre_norm_mp4.resolve())],
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=300,
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
                    str(pre_norm_mp4.resolve()),
                ],
                cwd=str(work_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=300,
            )
            stderr_used = fb.stderr or ""
            if fb.returncode != 0:
                raise RuntimeError(f"FFmpeg render failed: {stderr_used[-300:]}")

        norm_info = normalize_audio_ebu_r128(
            pre_norm_mp4,
            out_mp4,
            target_lufs=target_lufs,
            true_peak=target_tp,
            sample_rate=sample_rate,
        )
        print(
            f"[RENDERER] layout={layout['layout_mode']} card={layout.get('webcam_card_size')} "
            f"cam_upscale={layout['webcam_upscale_factor']}x no_overlap={layout['no_webcam_gameplay_overlap']} "
            f"cfr_fps={target_fps} scene_cuts={len(mapped_scene_cuts)} punch_ins={len(punch_windows)} "
            f"callout_mode={callout_mode if has_callout else 'none'} sfx_tracks={len(sfx_inputs)} "
            f"dead_air_engine={ae_info.get('provider') if ae_info.get('used') else 'pcm_fallback'} "
            f"jump_segs={len(kept_segments)} bleeps={len(bleep_spans)} norm={norm_info}"
        )

        fonts_loaded = sorted(set(_FONTSELECT_RE.findall(stderr_used)))
        if fonts_loaded:
            print(f"[RENDERER] libass fontselect={fonts_loaded[:4]}")

        if clean_cover_out is not None:
            from src.callout import _compose_adaptive_9_16
            from src.design_engine import select_peak_cover_frame_from_dir

            clean_frames_dir = work_dir / "clean_cover_frames"
            clean_frames_dir.mkdir(parents=True, exist_ok=True)
            # Sample 8 timestamps strictly along the KEPT output timeline and remap back to source_slice offsets!
            out_samples = np.linspace(min(0.6, out_dur * 0.08), max(0.9, out_dur - 0.8), 8)
            for idx, out_t in enumerate(out_samples):
                rel_s = map_output_ts_to_source_offset(float(out_t), kept_segments)
                raw_p = clean_frames_dir / f"raw_{idx:02d}.png"
                subprocess.run(
                    [
                        ffmpeg_exe,
                        "-y",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-ss",
                        f"{edit_plan.start_trim + float(rel_s):.3f}",
                        "-i",
                        str(source_slice.resolve()),
                        "-frames:v",
                        "1",
                        str(raw_p),
                    ],
                    capture_output=True,
                    check=False,
                    timeout=20,
                )
                if raw_p.is_file():
                    with Image.open(raw_p) as _im:
                        raw_img = _im.convert("RGBA")
                    composed_9_16 = _compose_adaptive_9_16(raw_img, layout, edit_plan.webcam_box_px).convert("RGB")
                    composed_9_16.save(clean_frames_dir / f"clean_{idx:02d}.jpg", format="JPEG", quality=94, subsampling=0)
                    raw_p.unlink(missing_ok=True)

            cov_meta = select_peak_cover_frame_from_dir(
                frames_dir=clean_frames_dir,
                out_clean_jpg=clean_cover_out,
                video_for_rms=out_mp4,
                duration_sec=out_dur,
                rank_offset=cover_rank_offset,
            )
            clean_cover_out.with_suffix(".meta.json").write_text(
                json.dumps(cov_meta, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            print(
                f"[RENDERER_CLEAN_COVER] pre_libass={clean_cover_out.name} "
                f"ts_ms={cov_meta['cover_timestamp_ms']} face={cov_meta['face_score']} rank_offset={cover_rank_offset}"
            )
    finally:
        if auto_cleanup:
            shutil.rmtree(work_dir, ignore_errors=True)
        enforce_runs_quota(RUNS_DIR, protect_dir=out_mp4.parent)

    return out_mp4
