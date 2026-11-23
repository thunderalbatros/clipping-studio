"""Editorial Visual Packaging & Cover Design Engine (v3.1 — Method C 2-Layer, BiRefNet-lite MIT, Chrome Headless, APCA, FFT Saliency).

Key guarantees:
- Strictly KS-4 commercial compliant: `rembg` with `birefnet-general-lite` (MIT) ONLY; forbids `bria-rmbg` (CC-BY-NC).
- Method C Hybrid 2-Layer for 9:16 vertical covers (ZERO Frame-0 static plaques injected into MP4):
  * Layer 1 (`select_peak_cover_frame`): Laplacian sharpness + UltraFace expression + audio RMS -> `cover_timestamp_ms` + `*_cover_clean.jpg`.
  * Layer 2 (`render_editorial_cover_9_16`): HTML5/CSS3/SVG + Chrome/Edge Headless + base64 `Montserrat Black` -> exact `(1080, 1920)` JPG
    anchored in the webcam/gameplay seam inside the Universal 4-Network Safe Box `[x: 90..990, y: 420..1250]`.
- Native 16:9 YouTube Long-Form thumbnails (`render_editorial_thumbnail_16_9`): exact `(1280, 720)` from native `16:9` slices with
  `BiRefNet-lite` Text-Behind-Subject + warm rim-light + Guardrail #4 mask-coverage ROI fallback.
- Deterministic QA suite: `PIL.Image.size` verification, APCA `Lc >= 75` contrast gate, `*_squint_160px.jpg`,
  `*_crop_preview.jpg` (IG 3:4 + TikTok 1:1), and pure-NumPy FFT 2D `*_saliency_heatmap.jpg`.
"""

from __future__ import annotations

import base64
import html as html_lib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Any

import numpy as np
import onnxruntime as ort
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from src.miner import PROJECT_ROOT, probe_video_stream
from src.rag_engine import query_rag
from src.resource_guard import onnx_threads
from src.schemas import UNIVERSAL_SAFE_BOX_9_16
from src.speaker_guard import detect_faces_ultraface

BUNDLED_FONT_PATH = PROJECT_ROOT / "assets" / "fonts" / "Montserrat-Black.ttf"
REMBG_HOME = PROJECT_ROOT / "models" / "rembg"
ALLOWED_MATTING_MODEL = "birefnet-general-lite"
FORBIDDEN_MATTING_MODELS = frozenset({"bria-rmbg", "bria", "sam", "matanyone"})

# Re-exported Single Source of Truth Safe Box [48..888, 420..1248] (survives IG 3:4, TikTok 1:1, YT Shorts right buttons, FB UI)
IG_GRID_3_4_BOX = (0, 240, 1080, 1680)
TIKTOK_GRID_1_1_BOX = (0, 420, 1080, 1500)
FB_UI_SAFE_BOX = (65, 269, 1015, 1248)
SAFE_BOX_16_9 = (64, 54, 1216, 610)

_REMBG_SESSIONS: dict[str, Any] = {}
_FONT_B64_CACHE: str | None = None
_HEADLINE_STOPWORDS = frozenset(
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
        "bro",
        "gta",
        "vi",
        "gta6",
        "gtavi",
        "shorts",
    }
)
_COMPLEMENTARY_FALLBACK_HOOKS = (
    "INSTANT KARMA",
    "WAIT FOR IT",
    "ZERO CHANCE",
    "WORST TIMING",
    "NO ESCAPE",
)


def _content_words(text: str) -> set[str]:
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return {t for t in tokens if len(t) >= 3 and t not in _HEADLINE_STOPWORDS}


def ensure_complementary_cover_headline(
    headline: str,
    title: str = "",
    fallback_headlines: list[str] | None = None,
    max_words: int = 4,
) -> tuple[str, list[str]]:
    """Enforce Curiosity-Gap cover copy (2-4 words) with ZERO literal content-word overlap with `title`."""
    clean_primary = " ".join(headline.strip().split())
    title_words = _content_words(title)

    def _clean_short(cand: str) -> str:
        ws = [w for w in cand.strip().split() if w][:max_words]
        return " ".join(ws).upper()

    if not title_words:
        chosen = _clean_short(clean_primary) or "WAIT FOR IT"
        return chosen, []

    primary_short = _clean_short(clean_primary)
    if primary_short and not (_content_words(primary_short) & title_words):
        return primary_short, []

    candidates: list[str] = []
    if fallback_headlines:
        candidates.extend(fallback_headlines)
    candidates.extend(_COMPLEMENTARY_FALLBACK_HOOKS)

    for cand in candidates:
        short = _clean_short(cand)
        if not short:
            continue
        if len(short.split()) < 2:
            continue
        if not (_content_words(short) & title_words):
            return short, []

    return "WAIT FOR IT", []


def _query_design_ctr_rag(query: str, top_k: int = 3) -> list[dict[str, Any]]:
    """Consult local hybrid RAG (`design_ctr` domain) for CTR / APCA / Safe-Box guidance."""
    try:
        hits = query_rag(query, domain="design_ctr", top_k=top_k, use_vectors=True)
        return [
            {
                "chunk_id": str(h.get("chunk_id") or ""),
                "heading": str(h.get("heading") or ""),
                "rrf_score": float(h.get("rrf_score") or 0.0),
            }
            for h in hits
        ]
    except Exception:
        return []


def select_peak_cover_frame_from_dir(
    frames_dir: Path,
    out_clean_jpg: Path,
    video_for_rms: Path | None = None,
    duration_sec: float = 0.0,
) -> dict[str, Any]:
    """Score pre-`libass` 9:16 frames extracted directly from `[base]` (ZERO burned captions)."""
    out_clean_jpg.parent.mkdir(parents=True, exist_ok=True)
    frame_paths = sorted(frames_dir.glob("clean_*.jpg"))
    if not frame_paths:
        raise RuntimeError(f"No pre-libass clean cover frames found in {frames_dir}")

    dur = max(1.0, float(duration_sec))
    n = len(frame_paths)
    timestamps = [round((idx + 0.5) * (dur / float(n)), 3) for idx in range(n)]
    rms_norms = (
        _extract_audio_rms_series(video_for_rms, timestamps)
        if (video_for_rms is not None and video_for_rms.is_file())
        else [0.5] * n
    )

    candidates: list[dict[str, Any]] = []
    for idx, fp in enumerate(frame_paths):
        with Image.open(fp) as _im:
            img = _im.convert("RGB")
        sharp = _laplacian_sharpness(img)
        luma_mean = float(np.asarray(img.convert("L"), dtype=np.float32).mean())
        if luma_mean < 16.0:
            sharp *= 0.1

        iw, ih = img.size
        face_roi = img.crop((0, 0, iw, min(ih, int(ih * 0.42)))) if ih > iw else img
        face_score = 0.0
        try:
            faces = detect_faces_ultraface(np.asarray(face_roi, dtype=np.uint8), confidence=0.50)
            if faces:
                best = max(faces, key=lambda b: b[4] * (b[2] - b[0]) * (b[3] - b[1]))
                face_score = float(best[4])
        except Exception:
            face_score = 0.0

        candidates.append(
            {
                "ts": timestamps[idx],
                "path": fp,
                "sharpness": sharp,
                "face_score": face_score,
                "rms_norm": rms_norms[idx] if idx < len(rms_norms) else 0.5,
            }
        )

    max_sharp = max(c["sharpness"] for c in candidates) or 1.0
    for c in candidates:
        c["total_score"] = round(0.45 * (c["sharpness"] / max_sharp) + 0.35 * c["face_score"] + 0.20 * c["rms_norm"], 4)

    best_c = max(candidates, key=lambda c: c["total_score"])
    with Image.open(best_c["path"]) as _im:
        best_img = _im.convert("RGB")
    best_img.save(out_clean_jpg, format="JPEG", quality=94, subsampling=0)
    return {
        "cover_timestamp_ms": round(float(best_c["ts"]) * 1000.0),
        "cover_clean_path": str(out_clean_jpg),
        "width": best_img.size[0],
        "height": best_img.size[1],
        "sharpness": round(float(best_c["sharpness"]), 2),
        "face_score": round(float(best_c["face_score"]), 3),
        "rms_norm": round(float(best_c["rms_norm"]), 3),
        "total_score": best_c["total_score"],
        "pre_libass": True,
    }


def _get_font_base64() -> str:
    global _FONT_B64_CACHE
    if _FONT_B64_CACHE is None:
        if not BUNDLED_FONT_PATH.is_file():
            raise FileNotFoundError(f"Bundled Montserrat-Black.ttf missing at {BUNDLED_FONT_PATH}")
        _FONT_B64_CACHE = base64.b64encode(BUNDLED_FONT_PATH.read_bytes()).decode("ascii")
    return _FONT_B64_CACHE


def find_headless_browser() -> Path | None:
    """Locate Chrome or Edge binary already installed on Windows (0 MB extra downloads)."""
    candidates = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(os.getenv("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    ]
    for p in candidates:
        if p.is_file():
            return p
    for name in ("chrome", "google-chrome", "msedge", "chromium"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def compute_apca_lc(fg_rgb: tuple[int, int, int], bg_rgb: tuple[int, int, int]) -> float:
    """Compute perceptual contrast APCA Lc (SAPC 0.0.98G-4g). Target |Lc| >= 75 for display typography."""

    def _y(rgb: tuple[int, int, int]) -> float:
        r, g, b = (max(0, min(255, int(c))) / 255.0 for c in rgb)
        lum = 0.2126729 * (r**2.4) + 0.7151522 * (g**2.4) + 0.0721750 * (b**2.4)
        if lum < 0.022:
            lum += (0.022 - lum) ** 1.414
        return lum

    y_txt = _y(fg_rgb)
    y_bg = _y(bg_rgb)
    if abs(y_bg - y_txt) < 0.0005:
        return 0.0
    if y_bg > y_txt:
        sapc = ((y_bg**0.56) - (y_txt**0.57)) * 1.14
        lc = 0.0 if sapc < 0.1 else (sapc - 0.027) * 100.0
    else:
        sapc = ((y_bg**0.65) - (y_txt**0.62)) * 1.14
        lc = 0.0 if sapc > -0.1 else (sapc + 0.027) * 100.0
    return round(float(lc), 2)


def _sample_region_avg_rgb(img: Image.Image, box: tuple[int, int, int, int]) -> tuple[int, int, int]:
    w, h = img.size
    x1 = max(0, min(w - 1, box[0]))
    y1 = max(0, min(h - 1, box[1]))
    x2 = max(x1 + 1, min(w, box[2]))
    y2 = max(y1 + 1, min(h, box[3]))
    arr = np.asarray(img.crop((x1, y1, x2, y2)).convert("RGB"), dtype=np.float32)
    mean = arr.mean(axis=(0, 1))
    return (int(mean[0]), int(mean[1]), int(mean[2]))


def _blend_over_scrim(bg_rgb: tuple[int, int, int], scrim_rgb: tuple[int, int, int], alpha: float) -> tuple[int, int, int]:
    return (
        round(bg_rgb[0] * (1.0 - alpha) + scrim_rgb[0] * alpha),
        round(bg_rgb[1] * (1.0 - alpha) + scrim_rgb[1] * alpha),
        round(bg_rgb[2] * (1.0 - alpha) + scrim_rgb[2] * alpha),
    )


def _ensure_apca_scrim_alpha(
    bg_rgb: tuple[int, int, int],
    fg_rgb: tuple[int, int, int] = (248, 250, 252),
    min_abs_lc: float = 75.0,
    base_alpha: float = 0.62,
) -> tuple[float, float]:
    """Increase local dark scrim alpha programmatically until |APCA Lc| >= min_abs_lc."""
    scrim = (8, 10, 16)
    alpha = base_alpha
    while alpha < 0.94:
        eff_bg = _blend_over_scrim(bg_rgb, scrim, alpha)
        lc = abs(compute_apca_lc(fg_rgb, eff_bg))
        if lc >= min_abs_lc:
            return round(alpha, 2), round(lc, 2)
        alpha = min(0.94, alpha + 0.06)
    eff_bg = _blend_over_scrim(bg_rgb, scrim, alpha)
    return round(alpha, 2), round(abs(compute_apca_lc(fg_rgb, eff_bg)), 2)


def _laplacian_sharpness(img: Image.Image) -> float:
    """Fast pure-NumPy 3x3 discrete Laplacian variance for frame sharpness scoring."""
    gray = np.asarray(img.convert("L").resize((360, 360), Image.Resampling.BILINEAR), dtype=np.float32)
    lap = (
        -4.0 * gray[1:-1, 1:-1]
        + gray[:-2, 1:-1]
        + gray[2:, 1:-1]
        + gray[1:-1, :-2]
        + gray[1:-1, 2:]
    )
    return float(np.var(lap))


def _extract_frame_at_sec(video_path: Path, ts_sec: float, out_path: Path) -> Path:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, ts_sec):.3f}",
            "-i",
            str(video_path.resolve()),
            "-frames:v",
            "1",
            str(out_path),
        ],
        capture_output=True,
        check=True,
        timeout=30,
    )
    return out_path


def _extract_audio_rms_series(video_path: Path, timestamps: list[float]) -> list[float]:
    """Measure normalized local RMS energy around each candidate timestamp."""
    if not timestamps:
        return []
    with tempfile.TemporaryDirectory(prefix="cs_cov_rms_") as tmp:
        wav_p = Path(tmp) / "a.wav"
        res = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(video_path.resolve()),
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
            timeout=30,
        )
        if res.returncode != 0 or not wav_p.exists():
            return [0.5] * len(timestamps)
        with wave.open(str(wav_p), "rb") as wf:
            raw = wf.readframes(wf.getnframes())
    if not raw:
        return [0.5] * len(timestamps)
    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    sr = 16000
    rms_vals: list[float] = []
    for ts in timestamps:
        c = int(ts * sr)
        s0 = max(0, c - sr // 4)
        s1 = min(len(pcm), c + sr // 4)
        seg = pcm[s0:s1]
        val = float(np.sqrt(np.mean(seg * seg) + 1e-12)) if len(seg) > 0 else 0.0
        rms_vals.append(val)
    peak = max(rms_vals) or 1.0
    return [round(v / peak, 4) for v in rms_vals]


def select_peak_cover_frame(
    video_path: Path,
    out_clean_jpg: Path,
    webcam_box_px: tuple[int, int, int, int] | None = None,
    num_candidates: int = 8,
) -> dict[str, Any]:
    """Layer 1 (Method C): score candidate frames by Laplacian sharpness + UltraFace expression + RMS energy."""
    out_clean_jpg.parent.mkdir(parents=True, exist_ok=True)
    w, h, _, dur = probe_video_stream(video_path)
    dur = max(1.0, dur)
    start_s = min(0.8, dur * 0.1)
    end_s = max(start_s + 0.4, dur - min(0.9, dur * 0.1))
    timestamps = [round(float(t), 3) for t in np.linspace(start_s, end_s, max(3, num_candidates))]
    rms_norms = _extract_audio_rms_series(video_path, timestamps)

    candidates: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="cs_peak_frame_") as tmp:
        tmp_dir = Path(tmp)
        for idx, ts in enumerate(timestamps):
            fp = tmp_dir / f"cand_{idx:02d}.png"
            try:
                _extract_frame_at_sec(video_path, ts, fp)
                with Image.open(fp) as _im:
                    img = _im.convert("RGB")
            except Exception:
                continue

            sharp = _laplacian_sharpness(img)
            # Brightness sanity (reject black transition frames)
            luma_mean = float(np.asarray(img.convert("L"), dtype=np.float32).mean())
            if luma_mean < 16.0:
                sharp *= 0.1

            # Face score via UltraFace on webcam region (or top band for 9:16)
            iw, ih = img.size
            if webcam_box_px is not None:
                wx, wy, ww, wh = webcam_box_px
                face_roi = img.crop((max(0, wx), max(0, wy), min(iw, wx + ww), min(ih, wy + wh)))
            elif ih > iw:
                face_roi = img.crop((0, 0, iw, min(ih, int(ih * 0.42))))
            else:
                face_roi = img

            face_score = 0.0
            try:
                faces = detect_faces_ultraface(np.asarray(face_roi, dtype=np.uint8), confidence=0.50)
                if faces:
                    best = max(faces, key=lambda b: b[4] * (b[2] - b[0]) * (b[3] - b[1]))
                    face_score = float(best[4])
            except Exception:
                face_score = 0.0

            candidates.append(
                {
                    "ts": ts,
                    "path": fp,
                    "sharpness": sharp,
                    "face_score": face_score,
                    "rms_norm": rms_norms[idx] if idx < len(rms_norms) else 0.5,
                    "luma": luma_mean,
                }
            )

        if not candidates:
            raise RuntimeError(f"Could not extract any candidate cover frame from {video_path}")

        max_sharp = max(c["sharpness"] for c in candidates) or 1.0
        for c in candidates:
            sharp_norm = c["sharpness"] / max_sharp
            c["total_score"] = round(0.45 * sharp_norm + 0.35 * c["face_score"] + 0.20 * c["rms_norm"], 4)

        best_c = max(candidates, key=lambda c: c["total_score"])
        with Image.open(best_c["path"]) as _im:
            best_img = _im.convert("RGB")
        best_img.save(out_clean_jpg, format="JPEG", quality=94, subsampling=0)

    return {
        "cover_timestamp_ms": round(float(best_c["ts"]) * 1000.0),
        "cover_clean_path": str(out_clean_jpg),
        "width": best_img.size[0],
        "height": best_img.size[1],
        "sharpness": round(float(best_c["sharpness"]), 2),
        "face_score": round(float(best_c["face_score"]), 3),
        "rms_norm": round(float(best_c["rms_norm"]), 3),
        "total_score": best_c["total_score"],
        "source_video_dims": [w, h],
    }


def _get_rembg_session(model_name: str = ALLOWED_MATTING_MODEL) -> Any:
    """Return cached rembg session strictly restricted to KS-4 commercial MIT model."""
    low = model_name.strip().lower()
    if low != ALLOWED_MATTING_MODEL or any(f in low for f in FORBIDDEN_MATTING_MODELS):
        raise ValueError(
            f"KS-4 license violation: model {model_name!r} forbidden. "
            f"Only commercial MIT '{ALLOWED_MATTING_MODEL}' is allowed."
        )
    if low not in _REMBG_SESSIONS:
        REMBG_HOME.mkdir(parents=True, exist_ok=True)
        os.environ["U2NET_HOME"] = str(REMBG_HOME.resolve())
        from rembg import new_session

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = onnx_threads()
        opts.inter_op_num_threads = 1
        opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        opts.enable_cpu_mem_arena = False
        opts.enable_mem_pattern = False
        opts.log_severity_level = 3
        _REMBG_SESSIONS[low] = new_session(low, sess_options=opts, providers=["CPUExecutionProvider"])
    return _REMBG_SESSIONS[low]


def _auto_detect_portrait_roi(img_rgb: Image.Image) -> tuple[int, int, int, int] | None:
    """Find the most prominent face via UltraFace and expand into a clean head+shoulders portrait ROI."""
    iw, ih = img_rgb.size
    try:
        faces = detect_faces_ultraface(np.asarray(img_rgb, dtype=np.uint8), confidence=0.52)
    except Exception:
        return None
    if not faces:
        return None
    best = max(faces, key=lambda b: b[4] * (b[2] - b[0]) * (b[3] - b[1]))
    x1, y1, x2, y2, _ = best
    fx1, fy1, fx2, fy2 = int(x1 * iw), int(y1 * ih), int(x2 * iw), int(y2 * ih)
    fw, fh = max(32, fx2 - fx1), max(32, fy2 - fy1)
    cx, cy = (fx1 + fx2) // 2, (fy1 + fy2) // 2
    rx1 = max(0, cx - int(fw * 1.45))
    ry1 = max(0, cy - int(fh * 0.95))
    rx2 = min(iw, cx + int(fw * 1.45))
    ry2 = min(ih, cy + int(fh * 2.20))
    if rx2 - rx1 < 64 or ry2 - ry1 < 64:
        return None
    return (rx1, ry1, rx2, ry2)


def extract_subject_cutout(
    img_rgb: Image.Image,
    webcam_box_px: tuple[int, int, int, int] | None = None,
    model_name: str = ALLOWED_MATTING_MODEL,
    min_coverage: float = 0.04,
    max_coverage: float = 0.72,
    rembg_fn: Any = None,
) -> tuple[Image.Image, dict[str, Any]]:
    """Extract subject RGBA cutout via `rembg` + `birefnet-general-lite` (MIT) with Guardrail #4 coverage fallback.

    - If `webcam_box_px` is supplied on a 16:9 stream frame, isolates the streamer portrait directly from that ROI
      for razor-sharp cutout quality without urban GTA VI background contamination.
    - Otherwise runs `birefnet-general-lite` on the full image and checks alpha mask coverage (`0.04..0.72`).
      If coverage exceeds `0.72` (engulfed too much complex GTA VI background) or falls below `0.04`,
      falls back cleanly to an UltraFace portrait ROI pass (or soft portrait vignette fallback).
    """
    low = model_name.strip().lower()
    if low != ALLOWED_MATTING_MODEL or any(f in low for f in FORBIDDEN_MATTING_MODELS):
        raise ValueError(
            f"KS-4 license violation: model {model_name!r} forbidden. "
            f"Only commercial MIT '{ALLOWED_MATTING_MODEL}' is allowed."
        )

    def _run_matting(pil_img: Image.Image) -> Image.Image | None:
        if rembg_fn is not None:
            return rembg_fn(pil_img).convert("RGBA")
        import gc

        from rembg import remove

        gc.collect()
        try:
            sess = _get_rembg_session(low)
            out = remove(pil_img, session=sess)
            if isinstance(out, bytes):
                return Image.open(io.BytesIO(out)).convert("RGBA")
            if isinstance(out, Image.Image):
                return out.convert("RGBA")
            return Image.fromarray(out).convert("RGBA")
        except Exception:
            return None

    iw, ih = img_rgb.size
    used_roi_fallback = False
    roi_box: tuple[int, int, int, int] | None = None

    # Case A: Explicit webcam PiP crop on 16:9 stream -> direct high-precision streamer portrait matting
    if webcam_box_px is not None:
        wx, wy, ww, wh = webcam_box_px
        rx1, ry1 = max(0, wx), max(0, wy)
        rx2, ry2 = min(iw, wx + ww), min(ih, wy + wh)
        if rx2 - rx1 >= 64 and ry2 - ry1 >= 64:
            crop = img_rgb.crop((rx1, ry1, rx2, ry2))
            rgba = _run_matting(crop)
            cov = 0.0
            if rgba is not None:
                alpha = np.asarray(rgba.getchannel("A"), dtype=np.uint8)
                cov = round(float(np.mean(alpha > 128)), 4)
            if rgba is None or cov < min_coverage:
                # Soft feathered oval fallback inside webcam crop if matting missed face or hit RAM limit
                used_roi_fallback = True
                mask = Image.new("L", crop.size, 0)
                cw, ch = crop.size
                ImageDraw.Draw(mask).ellipse([int(cw * 0.08), int(ch * 0.02), int(cw * 0.92), int(ch * 1.05)], fill=255)
                mask = mask.filter(ImageFilter.GaussianBlur(radius=12))
                rgba = crop.convert("RGBA")
                rgba.putalpha(mask)
                cov = round(float(np.mean(np.asarray(mask) > 128)), 4)
            return rgba, {
                "model": low,
                "mode": "webcam_roi",
                "mask_coverage": cov,
                "used_roi_fallback": used_roi_fallback,
                "roi_box": [rx1, ry1, rx2, ry2],
            }

    # Case B: Full-frame matting with Guardrail #4 coverage audit
    full_rgba = _run_matting(img_rgb)
    raw_cov = 0.0
    if full_rgba is not None:
        alpha_full = np.asarray(full_rgba.getchannel("A"), dtype=np.uint8)
        raw_cov = round(float(np.mean(alpha_full > 128)), 4)

    if full_rgba is not None and min_coverage <= raw_cov <= max_coverage:
        return full_rgba, {
            "model": low,
            "mode": "full_frame",
            "mask_coverage": raw_cov,
            "used_roi_fallback": False,
            "roi_box": [0, 0, iw, ih],
        }

    # Guardrail #4 Triggered: full-frame mask engulfed too much GTA VI background (>0.72) or missed (<0.04)
    used_roi_fallback = True
    roi_box = _auto_detect_portrait_roi(img_rgb)
    if roi_box is not None:
        rx1, ry1, rx2, ry2 = roi_box
        crop = img_rgb.crop((rx1, ry1, rx2, ry2))
        roi_rgba = _run_matting(crop)
        if roi_rgba is not None:
            roi_alpha = np.asarray(roi_rgba.getchannel("A"), dtype=np.uint8)
            roi_cov = round(float(np.mean(roi_alpha > 128)), 4)
            if roi_cov >= min_coverage:
                return roi_rgba, {
                    "model": low,
                    "mode": "ultraface_roi_fallback",
                    "raw_full_coverage": raw_cov,
                    "mask_coverage": roi_cov,
                    "used_roi_fallback": True,
                    "roi_box": [rx1, ry1, rx2, ry2],
                }

    # Ultimate clean fallback: center-right foreground soft portrait cutout (never returns opaque full-frame blob)
    rx1, ry1, rx2, ry2 = int(iw * 0.56), int(ih * 0.30), iw, ih
    crop = img_rgb.crop((rx1, ry1, rx2, ry2))
    mask = Image.new("L", crop.size, 0)
    cw, ch = crop.size
    ImageDraw.Draw(mask).ellipse([int(cw * 0.08), int(ch * 0.04), int(cw * 0.94), int(ch * 1.06)], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(radius=14))
    rgba_fb = crop.convert("RGBA")
    rgba_fb.putalpha(mask)
    fb_cov = round(float(np.mean(np.asarray(mask) > 128)), 4)
    return rgba_fb, {
        "model": low,
        "mode": "soft_roi_fallback",
        "raw_full_coverage": raw_cov,
        "mask_coverage": fb_cov,
        "used_roi_fallback": True,
        "roi_box": [rx1, ry1, rx2, ry2],
    }


def _inferno_colormap(norm_2d: np.ndarray) -> np.ndarray:
    """Fast pure-NumPy inferno-inspired RGB colormap for saliency heatmap overlays."""
    v = np.clip(norm_2d, 0.0, 1.0)
    r = np.clip(1.6 * v + 0.4 * np.sin(np.pi * v), 0.0, 1.0)
    g = np.clip((v**1.8) * 1.15 - 0.15 * (1.0 - v), 0.0, 1.0)
    b = np.clip(0.55 * np.sin(np.pi * (v**0.7)) * (1.0 - 0.65 * v), 0.0, 1.0)
    rgb = np.stack([r, g, b], axis=-1)
    return (rgb * 255.0).astype(np.uint8)


def compute_spectral_saliency(
    img_rgb: Image.Image,
    safe_box: tuple[int, int, int, int] = UNIVERSAL_SAFE_BOX_9_16,
    out_heatmap_jpg: Path | None = None,
    apca_lc: float | None = None,
) -> dict[str, Any]:
    """Compute Spectral Residual Saliency via 2D FFT (Hou & Zhang, <50ms CPU) and optional QA heatmap JPG."""
    w, h = img_rgb.size
    # Preserve aspect ratio around 128px max dimension for fast FFT (<15ms CPU)
    if h >= w:
        sh = 128
        sw = max(32, round(128 * (w / float(h))))
    else:
        sw = 128
        sh = max(32, round(128 * (h / float(w))))

    gray = np.asarray(img_rgb.convert("L").resize((sw, sh), Image.Resampling.BILINEAR), dtype=np.float32) / 255.0
    fft = np.fft.fft2(gray)
    log_amp = np.log(np.abs(fft) + 1e-8)
    phase = np.angle(fft)

    # 3x3 box average via padded slicing (zero SciPy overhead)
    padded = np.pad(log_amp, 1, mode="edge")
    avg_log_amp = (
        padded[:-2, :-2]
        + padded[:-2, 1:-1]
        + padded[:-2, 2:]
        + padded[1:-1, :-2]
        + padded[1:-1, 1:-1]
        + padded[1:-1, 2:]
        + padded[2:, :-2]
        + padded[2:, 1:-1]
        + padded[2:, 2:]
    ) / 9.0
    spectral_residual = log_amp - avg_log_amp
    sal = np.abs(np.fft.ifft2(np.exp(spectral_residual + 1j * phase))) ** 2

    # Smooth saliency map cleanly via Pillow GaussianBlur and normalize to [0, 1]
    sal_max = float(sal.max()) or 1.0
    sal_u8 = np.clip((sal / sal_max) * 255.0, 0, 255).astype(np.uint8)
    sal_smooth_img = Image.fromarray(sal_u8, mode="L").filter(ImageFilter.GaussianBlur(radius=4))
    sal_full = np.asarray(sal_smooth_img.resize((w, h), Image.Resampling.BILINEAR), dtype=np.float32)
    sal_full = sal_full / (float(sal_full.max()) or 1.0)

    # Emphasize high-salience attention peaks (top energy squared)
    weights = sal_full**2
    total_energy = float(weights.sum()) + 1e-9
    sx1, sy1, sx2, sy2 = (
        max(0, min(w, safe_box[0])),
        max(0, min(h, safe_box[1])),
        max(0, min(w, safe_box[2])),
        max(0, min(h, safe_box[3])),
    )
    safe_energy = float(weights[sy1:sy2, sx1:sx2].sum())
    safe_ratio = round(safe_energy / total_energy, 4)

    ys, xs = np.mgrid[0:h, 0:w]
    cx = float((xs * weights).sum() / total_energy)
    cy = float((ys * weights).sum() / total_energy)
    centroid_in_safe = bool(sx1 <= cx <= sx2 and sy1 <= cy <= sy2)

    if out_heatmap_jpg is not None:
        out_heatmap_jpg.parent.mkdir(parents=True, exist_ok=True)
        heat_rgb = _inferno_colormap(sal_full)
        base_arr = np.asarray(img_rgb.convert("RGB"), dtype=np.float32)
        blended = np.clip(base_arr * 0.52 + heat_rgb.astype(np.float32) * 0.48, 0, 255).astype(np.uint8)
        overlay_img = Image.fromarray(blended, mode="RGB")
        draw = ImageDraw.Draw(overlay_img)
        draw.rectangle([sx1, sy1, sx2, sy2], outline=(64, 255, 128), width=5)
        icx, icy = round(cx), round(cy)
        draw.ellipse([icx - 18, icy - 18, icx + 18, icy + 18], outline=(255, 255, 255), width=4)
        draw.line([(icx - 28, icy), (icx + 28, icy)], fill=(255, 255, 255), width=3)
        draw.line([(icx, icy - 28), (icx, icy + 28)], fill=(255, 255, 255), width=3)

        # Compact metric header bar
        badge_font = (
            ImageFont.truetype(str(BUNDLED_FONT_PATH), 24) if BUNDLED_FONT_PATH.is_file() else ImageFont.load_default()
        )
        label = f"SALIENCY SAFE: {safe_ratio * 100:.1f}%"
        if apca_lc is not None:
            label += f" | APCA Lc: {apca_lc:.1f}"
        draw.rectangle([20, 20, min(w - 20, 20 + len(label) * 16 + 24), 66], fill=(10, 12, 18))
        draw.text((32, 29), label, font=badge_font, fill=(255, 224, 64))
        overlay_img.save(out_heatmap_jpg, format="JPEG", quality=90)

    return {
        "safe_box_energy_ratio": safe_ratio,
        "centroid_px": [round(cx, 1), round(cy, 1)],
        "centroid_norm": [round(cx / float(w), 4), round(cy / float(h), 4)],
        "centroid_in_safe_box": centroid_in_safe,
        "heatmap_path": str(out_heatmap_jpg) if out_heatmap_jpg else "",
    }


def render_squint_test(img_rgb: Image.Image, out_jpg: Path, target_width: int = 160) -> Path:
    """Emit `*_squint_160px.jpg` simulating real mobile feed size for instant legibility QA."""
    out_jpg.parent.mkdir(parents=True, exist_ok=True)
    w, h = img_rgb.size
    target_h = max(1, round(h * (target_width / float(w))))
    squint = img_rgb.resize((target_width, target_h), Image.Resampling.LANCZOS)
    squint.save(out_jpg, format="JPEG", quality=88)
    return out_jpg


def render_crop_preview(img_rgb: Image.Image, out_jpg: Path, fmt: str = "9_16") -> Path:
    """Emit `*_crop_preview.jpg` validating IG 3:4 & TikTok 1:1 simultaneously (or 16:9 Rule-of-Thirds + YT badge)."""
    out_jpg.parent.mkdir(parents=True, exist_ok=True)
    font = ImageFont.truetype(str(BUNDLED_FONT_PATH), 22) if BUNDLED_FONT_PATH.is_file() else ImageFont.load_default()

    if fmt == "9_16":
        # 3-panel board: [Full 9:16 + Safe Guides] | [IG 3:4 Grid Crop] | [TikTok 1:1 Grid Crop]
        panel_h = 960
        full_p = img_rgb.copy()
        d = ImageDraw.Draw(full_p)
        d.rectangle(IG_GRID_3_4_BOX, outline=(255, 180, 0), width=6)
        d.rectangle(TIKTOK_GRID_1_1_BOX, outline=(0, 229, 255), width=6)
        d.rectangle(UNIVERSAL_SAFE_BOX_9_16, outline=(64, 255, 128), width=6)
        full_thumb = full_p.resize((540, panel_h), Image.Resampling.LANCZOS)

        ig_crop = img_rgb.crop(IG_GRID_3_4_BOX).resize((540, 720), Image.Resampling.LANCZOS)
        tt_crop = img_rgb.crop(TIKTOK_GRID_1_1_BOX).resize((540, 540), Image.Resampling.LANCZOS)

        board = Image.new("RGB", (1700, panel_h + 70), (14, 16, 22))
        bd = ImageDraw.Draw(board)
        board.paste(full_thumb, (20, 54))
        board.paste(ig_crop, (580, 54 + (panel_h - 720) // 2))
        board.paste(tt_crop, (1140, 54 + (panel_h - 540) // 2))

        bd.text((24, 16), "FULL 9:16 + UNIVERSAL SAFE BOX (GREEN)", font=font, fill=(64, 255, 128))
        bd.text((584, 16), "INSTAGRAM PROFILE GRID (3:4)", font=font, fill=(255, 196, 0))
        bd.text((1144, 16), "TIKTOK PROFILE GRID (1:1)", font=font, fill=(0, 229, 255))
        board.save(out_jpg, format="JPEG", quality=91)
        return out_jpg

    # 16:9 QA board: Rule-of-Thirds lines + YouTube bottom-right duration pill zone
    guide = img_rgb.copy()
    w, h = guide.size
    d = ImageDraw.Draw(guide)
    for frac in (1 / 3, 2 / 3):
        x = round(w * frac)
        y = round(h * frac)
        d.line([(x, 0), (x, h)], fill=(0, 229, 255), width=2)
        d.line([(0, y), (w, y)], fill=(0, 229, 255), width=2)
    d.rectangle(SAFE_BOX_16_9, outline=(64, 255, 128), width=4)
    # YouTube bottom-right timestamp badge occlusion warning zone
    d.rectangle([w - 175, h - 78, w - 18, h - 18], fill=(10, 10, 14), outline=(255, 80, 80), width=3)
    d.text((w - 146, h - 62), "10:24", font=font, fill=(255, 255, 255))
    guide.save(out_jpg, format="JPEG", quality=91)
    return out_jpg


def _img_to_data_uri(img: Image.Image, fmt: str = "PNG") -> str:
    buf = io.BytesIO()
    if fmt.upper() == "JPEG":
        img.convert("RGB").save(buf, format="JPEG", quality=93, subsampling=0)
        mime = "image/jpeg"
    else:
        img.save(buf, format="PNG")
        mime = "image/png"
    return f"data:{mime};base64,{base64.b64encode(buf.getvalue()).decode('ascii')}"


def _render_html_to_image(html_doc: str, width: int, height: int) -> tuple[Image.Image, str]:
    """Rasterize HTML5/CSS3/SVG via Chrome/Edge Headless and enforce exact (width, height) pixel output."""
    browser = find_headless_browser()
    if browser is None:
        raise RuntimeError("No Chrome or Edge browser binary found for headless rendering")

    with tempfile.TemporaryDirectory(prefix="cs_design_html_") as tmp:
        tmp_dir = Path(tmp)
        html_path = tmp_dir / "canvas.html"
        out_png = tmp_dir / "render.png"
        html_path.write_text(html_doc, encoding="utf-8")

        def _capture(win_w: int, win_h: int) -> Image.Image | None:
            cmd = [
                str(browser),
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                "--disable-software-rasterizer",
                "--disable-extensions",
                "--force-device-scale-factor=1",
                "--hide-scrollbars",
                f"--window-size={win_w},{win_h}",
                "--virtual-time-budget=2000",
                f"--screenshot={out_png}",
                html_path.as_uri(),
            ]
            subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=25)
            if out_png.is_file() and out_png.stat().st_size > 0:
                with Image.open(out_png) as _im:
                    return _im.convert("RGB")
            return None

        img = _capture(width, height)
        if img is None:
            raise RuntimeError(f"Headless browser {browser.name} failed to capture screenshot")

        # Guardrail #2: compensate if Windows chrome/DPI ever trims height below target
        if img.size != (width, height):
            if img.size[0] < width or img.size[1] < height:
                retry_img = _capture(width + 32, height + 180)
                if retry_img is not None and retry_img.size[0] >= width and retry_img.size[1] >= height:
                    img = retry_img.crop((0, 0, width, height))
                else:
                    img = img.resize((width, height), Image.Resampling.LANCZOS)
            else:
                img = img.crop((0, 0, width, height))

        if img.size != (width, height):
            raise RuntimeError(f"Guardrail #2 dimension mismatch: got {img.size}, expected {(width, height)}")
        return img, browser.name


def _format_headline_lines(headline: str, max_words: int = 4) -> list[str]:
    """Normalize headline to 2..4 crisp uppercase words split cleanly into 1-2 balanced lines."""
    clean = " ".join(headline.strip().split())
    words = [w.upper() for w in clean.split() if w][:max_words]
    if not words:
        words = ["GTA VI", "CHAOS"]
    if len(words) <= 2:
        return [" ".join(words)]
    mid = (len(words) + 1) // 2
    return [" ".join(words[:mid]), " ".join(words[mid:])]


def _highlight_accent_word_html(lines: list[str], accent_hex: str = "#FFD400") -> str:
    """Style lines in Montserrat Black highlighting the highest-impact final word in warm gold accent."""
    total_words = sum(len(ln.split()) for ln in lines)
    word_idx = 0
    html_lines: list[str] = []
    for ln in lines:
        spans: list[str] = []
        for w in ln.split():
            word_idx += 1
            esc = html_lib.escape(w)
            if word_idx == total_words and total_words >= 2:
                spans.append(f'<span class="accent" style="color:{accent_hex};">{esc}</span>')
            else:
                spans.append(esc)
        html_lines.append(f'<div class="hl-line">{" ".join(spans)}</div>')
    return "".join(html_lines)


def _load_source_image(
    video_or_img: Path,
    out_clean_jpg: Path | None = None,
    webcam_box_px: tuple[int, int, int, int] | None = None,
) -> tuple[Image.Image, dict[str, Any]]:
    """Load an image directly or run Layer-1 peak frame selection on a video file."""
    ext = video_or_img.suffix.lower()
    if ext in {".jpg", ".jpeg", ".png", ".webp"}:
        with Image.open(video_or_img) as _im:
            img = _im.convert("RGB")
        if out_clean_jpg is not None and out_clean_jpg.resolve() != video_or_img.resolve():
            out_clean_jpg.parent.mkdir(parents=True, exist_ok=True)
            img.save(out_clean_jpg, format="JPEG", quality=94, subsampling=0)
        ts_ms = 0
        sidecar_meta = video_or_img.with_suffix(".meta.json")
        if sidecar_meta.is_file():
            try:
                loaded_meta = json.loads(sidecar_meta.read_text(encoding="utf-8"))
                ts_ms = int(loaded_meta.get("cover_timestamp_ms") or 0)
            except Exception:
                ts_ms = 0
        return img, {
            "cover_timestamp_ms": ts_ms,
            "cover_clean_path": str(out_clean_jpg or video_or_img),
            "width": img.size[0],
            "height": img.size[1],
            "pre_libass": True,
        }
    clean_target = out_clean_jpg or video_or_img.with_name(f"{video_or_img.stem}_cover_clean.jpg")
    meta = select_peak_cover_frame(video_or_img, clean_target, webcam_box_px=webcam_box_px)
    with Image.open(clean_target) as _im:
        clean_img = _im.convert("RGB")
    return clean_img, meta


def render_editorial_cover_9_16(
    video_or_img: Path,
    headline: str,
    out_jpg: Path,
    badge_text: str = "GTA VI",
    webcam_box_px: tuple[int, int, int, int] | None = None,
    clean_cover_jpg: Path | None = None,
    title: str = "",
    fallback_headlines: list[str] | None = None,
    cover_timestamp_ms_override: int | None = None,
) -> dict[str, Any]:
    """Render Method-C Layer 2 `1080x1920` editorial cover with Universal 4-Network Safe Box, APCA >= 75, and QA artifacts."""
    if not BUNDLED_FONT_PATH.is_file():
        raise FileNotFoundError(f"Montserrat Black font required without fallback: {BUNDLED_FONT_PATH}")

    out_jpg.parent.mkdir(parents=True, exist_ok=True)
    clean_path = clean_cover_jpg or out_jpg.with_name(out_jpg.stem.replace("_cover_9_16", "") + "_cover_clean.jpg")
    base_img, layer1 = _load_source_image(video_or_img, out_clean_jpg=clean_path, webcam_box_px=webcam_box_px)
    if cover_timestamp_ms_override is not None and cover_timestamp_ms_override > 0:
        layer1["cover_timestamp_ms"] = int(cover_timestamp_ms_override)

    rag_hits = _query_design_ctr_rag(f"9:16 cover APCA safe box curiosity gap {headline} {badge_text}", top_k=3)
    final_headline, overlap_words = ensure_complementary_cover_headline(
        headline, title=title, fallback_headlines=fallback_headlines, max_words=4
    )

    # Fit exact 1080x1920 (center-crop preserving aspect ratio, zero distortion)
    target_w, target_h = 1080, 1920
    bw, bh = base_img.size
    if (bw, bh) != (target_w, target_h):
        scale = max(target_w / float(bw), target_h / float(bh))
        nw, nh = round(bw * scale), round(bh * scale)
        resized = base_img.resize((nw, nh), Image.Resampling.LANCZOS)
        ox, oy = (nw - target_w) // 2, (nh - target_h) // 2
        base_img = resized.crop((ox, oy, ox + target_w, oy + target_h))

    # Subtle editorial grade on photography
    graded = ImageEnhance.Contrast(base_img).enhance(1.07)
    graded = ImageEnhance.Color(graded).enhance(1.08)

    # Sample local seam band (y=650..870 inside Universal Safe Box) and enforce APCA Lc >= 75
    seam_bg_rgb = _sample_region_avg_rgb(graded, (120, 650, 960, 870))
    scrim_alpha, apca_lc = _ensure_apca_scrim_alpha(seam_bg_rgb, fg_rgb=(248, 250, 252), min_abs_lc=75.0, base_alpha=0.72)

    lines = _format_headline_lines(final_headline, max_words=4)
    longest_line = max(len(ln) for ln in lines)
    font_px = 112 if longest_line <= 10 else (96 if longest_line <= 14 else 82)
    headline_html = _highlight_accent_word_html(lines, accent_hex="#FFD400")
    badge_html = html_lib.escape(badge_text.strip().upper()[:14]) if badge_text.strip() else ""

    bg_uri = _img_to_data_uri(graded, fmt="JPEG")
    font_b64 = _get_font_base64()

    html_doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<style>
@font-face {{
  font-family: 'MontserratBlack';
  src: url('data:font/ttf;base64,{font_b64}') format('truetype');
  font-weight: 900;
  font-style: normal;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html, body {{
  width: 1080px;
  height: 1920px;
  overflow: hidden;
  background: #080a10;
  position: relative;
  font-family: 'MontserratBlack', sans-serif;
}}
.bg {{
  position: absolute;
  inset: 0;
  width: 1080px;
  height: 1920px;
  object-fit: cover;
}}
.vignette {{
  position: absolute;
  inset: 0;
  background:
    radial-gradient(ellipse 88% 72% at 50% 44%, rgba(8,10,16,0) 48%, rgba(6,8,14,0.52) 100%),
    linear-gradient(180deg, rgba(6,8,14,0.0) 0%, rgba(6,8,14,0.0) 29%, rgba(6,8,14,{scrim_alpha:.2f}) 38%, rgba(6,8,14,{min(0.92, scrim_alpha + 0.08):.2f}) 44%, rgba(6,8,14,0.0) 56%, rgba(6,8,14,0.34) 100%);
}}
.grain {{
  position: absolute;
  inset: 0;
  opacity: 0.05;
  mix-blend-mode: overlay;
  pointer-events: none;
}}
/* Anchored cleanly on webcam/gameplay seam (y ~ 670..860), strictly inside Universal Safe Box [48..888, 420..1248] */
.headline-wrap {{
  position: absolute;
  left: {UNIVERSAL_SAFE_BOX_9_16[0]}px;
  width: {UNIVERSAL_SAFE_BOX_9_16[2] - UNIVERSAL_SAFE_BOX_9_16[0]}px;
  top: 668px;
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  gap: 8px;
}}
.eyebrow {{
  display: inline-block;
  font-size: 24px;
  letter-spacing: 0.16em;
  color: #090B10;
  background: #FFD400;
  padding: 5px 18px 4px 18px;
  border-radius: 4px;
  margin-bottom: 4px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.55);
}}
.hl-line {{
  font-family: 'MontserratBlack', sans-serif;
  font-size: {font_px}px;
  line-height: 0.92;
  letter-spacing: -0.035em;
  color: #F8FAFC;
  text-transform: uppercase;
  text-shadow:
    0 6px 26px rgba(0, 0, 0, 0.92),
    0 2px 4px rgba(0, 0, 0, 0.96);
  -webkit-text-stroke: 2px rgba(6, 8, 14, 0.72);
  paint-order: stroke fill;
}}
.accent-rule {{
  width: 112px;
  height: 5px;
  background: linear-gradient(90deg, rgba(255,212,0,0) 0%, #FFD400 50%, rgba(255,212,0,0) 100%);
  margin-top: 6px;
}}
</style>
</head>
<body>
  <img class="bg" src="{bg_uri}" alt="" />
  <div class="vignette"></div>
  <svg class="grain" width="1080" height="1920" xmlns="http://www.w3.org/2000/svg">
    <filter id="g"><feTurbulence type="fractalNoise" baseFrequency="0.78" numOctaves="2" stitchTiles="stitch"/></filter>
    <rect width="1080" height="1920" filter="url(#g)" />
  </svg>
  <div class="headline-wrap">
    {f'<div class="eyebrow">{badge_html}</div>' if badge_html else ''}
    {headline_html}
    <div class="accent-rule"></div>
  </div>
</body>
</html>"""

    rendered_img, renderer_used = _render_html_to_image(html_doc, target_w, target_h)
    rendered_img.save(out_jpg, format="JPEG", quality=93, subsampling=0)
    with Image.open(out_jpg) as verify_img:
        verified_size = verify_img.size
    if verified_size != (1080, 1920):
        raise RuntimeError(f"Runtime PIL.Image.size check failed for 9:16 cover: got {verified_size}")

    # Emit mandatory deterministic QA artifacts
    stem = out_jpg.stem
    squint_jpg = out_jpg.with_name(f"{stem}_squint_160px.jpg")
    crop_jpg = out_jpg.with_name(f"{stem}_crop_preview.jpg")
    saliency_jpg = out_jpg.with_name(f"{stem}_saliency_heatmap.jpg")

    render_squint_test(rendered_img, squint_jpg, target_width=160)
    render_crop_preview(rendered_img, crop_jpg, fmt="9_16")
    saliency = compute_spectral_saliency(rendered_img, safe_box=UNIVERSAL_SAFE_BOX_9_16, out_heatmap_jpg=saliency_jpg, apca_lc=apca_lc)

    return {
        "format": "9_16",
        "cover_path": str(out_jpg),
        "cover_clean_path": layer1["cover_clean_path"],
        "cover_timestamp_ms": layer1["cover_timestamp_ms"],
        "pre_libass": bool(layer1.get("pre_libass", False)),
        "cover_headline_used": final_headline,
        "title_overlap_words": overlap_words,
        "dimensions": list(verified_size),
        "renderer": renderer_used,
        "font": "Montserrat-Black.ttf (embedded base64)",
        "apca_lc": apca_lc,
        "scrim_alpha": scrim_alpha,
        "safe_box": list(UNIVERSAL_SAFE_BOX_9_16),
        "rag_design_hits": rag_hits,
        "saliency": saliency,
        "qa_artifacts": {
            "squint_160px": str(squint_jpg),
            "crop_preview": str(crop_jpg),
            "saliency_heatmap": str(saliency_jpg),
        },
    }


def render_editorial_thumbnail_16_9(
    video_or_img: Path,
    headline: str,
    out_jpg: Path,
    badge_text: str = "GTA VI",
    webcam_box_px: tuple[int, int, int, int] | None = None,
    use_matting: bool = True,
    title: str = "",
    fallback_headlines: list[str] | None = None,
) -> dict[str, Any]:
    """Render native 16:9 `1280x720` YouTube thumbnail with `rembg/birefnet-general-lite` Text-Behind-Subject + QA artifacts."""
    if not BUNDLED_FONT_PATH.is_file():
        raise FileNotFoundError(f"Montserrat Black font required without fallback: {BUNDLED_FONT_PATH}")

    out_jpg.parent.mkdir(parents=True, exist_ok=True)
    rag_hits = _query_design_ctr_rag(f"16:9 YouTube thumbnail text behind subject APCA {headline} {badge_text}", top_k=3)
    final_headline, overlap_words = ensure_complementary_cover_headline(
        headline, title=title, fallback_headlines=fallback_headlines, max_words=4
    )
    clean_tmp = out_jpg.with_name(f"{out_jpg.stem}_source_frame.jpg")
    base_img, layer1 = _load_source_image(video_or_img, out_clean_jpg=clean_tmp, webcam_box_px=webcam_box_px)
    bw, bh = base_img.size

    # Strictly enforce landscape native 16:9 crop (zero vertical black pillarbox!)
    target_w, target_h = 1280, 720
    if bh > bw:
        # Fallback if a vertical image was accidentally passed: zoom-fill with blur depth (never hard black bars)
        bg_fill = base_img.resize((target_w, target_h), Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(14))
        bg_fill = ImageEnhance.Brightness(bg_fill).enhance(0.55)
        canvas = bg_fill
        scale_x = target_w / float(bw)
        scale_y = target_h / float(bh)
        scaled_cam = None
    else:
        scale = max(target_w / float(bw), target_h / float(bh))
        nw, nh = round(bw * scale), round(bh * scale)
        resized = base_img.resize((nw, nh), Image.Resampling.LANCZOS)
        ox, oy = (nw - target_w) // 2, (nh - target_h) // 2
        canvas = resized.crop((ox, oy, ox + target_w, oy + target_h))
        scale_x = scale
        scale_y = scale
        if webcam_box_px is not None:
            wx, wy, ww, wh = webcam_box_px
            scaled_cam = (
                max(0, round(wx * scale_x) - ox),
                max(0, round(wy * scale_y) - oy),
                round(ww * scale_x),
                round(wh * scale_y),
            )
        else:
            scaled_cam = None

    # Auto-detect corner streamer face if webcam_box_px wasn't explicitly provided on a 16:9 frame
    inferred_cam = scaled_cam
    if inferred_cam is None and bw >= bh:
        roi = _auto_detect_portrait_roi(canvas)
        if roi is not None:
            rx1, ry1, rx2, ry2 = roi
            # If face lies in right or left lower half (typical streamer PiP), isolate portrait cleanly
            inferred_cam = (rx1, ry1, rx2 - rx1, ry2 - ry1)

    # Subject matting via commercial MIT `birefnet-general-lite` with Guardrail #4 coverage audit
    cutout_uri = ""
    matting_meta: dict[str, Any] = {"ran": False}
    subject_on_right = True

    if use_matting:
        cutout_rgba, matting_meta = extract_subject_cutout(
            canvas,
            webcam_box_px=inferred_cam,
            model_name=ALLOWED_MATTING_MODEL,
        )
        # Trim transparent margins around subject cutout for crisp positioning
        bbox = cutout_rgba.getbbox()
        if bbox is not None:
            cutout_rgba = cutout_rgba.crop(bbox)
            # Determine side (default right-third composition, flip left only if webcam was firmly on left)
            roi_b = matting_meta.get("roi_box") or [0, 0, target_w, target_h]
            roi_cx = (roi_b[0] + roi_b[2]) * 0.5
            subject_on_right = roi_cx >= target_w * 0.42
            # Scale portrait heroically (~80% height of 720p canvas) with crisp sharpening
            hero_h = int(target_h * 0.80)
            hero_w = max(120, round(cutout_rgba.size[0] * (hero_h / float(max(1, cutout_rgba.size[1])))))
            if hero_w > int(target_w * 0.56):
                hero_w = int(target_w * 0.56)
                hero_h = max(120, round(cutout_rgba.size[1] * (hero_w / float(max(1, cutout_rgba.size[0])))))
            cutout_rgba = cutout_rgba.resize((hero_w, hero_h), Image.Resampling.LANCZOS)
            cutout_uri = _img_to_data_uri(cutout_rgba, fmt="PNG")

    # Treat background gameplay canvas: exclude original webcam box cleanly in exact 16:9 ratio
    # so zero duplicate corner head/chat overlay leaks behind the BiRefNet-lite subject cutout!
    bg_canvas = canvas
    if inferred_cam is not None and cutout_uri:
        cam_x, _cam_y, cam_w, _cam_h = inferred_cam
        if subject_on_right:
            avail_w = max(round(target_w * 0.62), min(target_w, cam_x - 8))
            crop_h = min(target_h, round(avail_w * (target_h / float(target_w))))
            crop_w = round(crop_h * (target_w / float(target_h)))
            crop_x = 0
            crop_y = max(0, min(target_h - crop_h, round((target_h - crop_h) * 0.42)))
        else:
            right_start = min(round(target_w * 0.38), cam_x + cam_w + 8)
            avail_w = max(round(target_w * 0.62), target_w - right_start)
            crop_h = min(target_h, round(avail_w * (target_h / float(target_w))))
            crop_w = round(crop_h * (target_w / float(target_h)))
            crop_x = max(0, target_w - crop_w)
            crop_y = max(0, min(target_h - crop_h, round((target_h - crop_h) * 0.42)))
        bg_canvas = canvas.crop((crop_x, crop_y, crop_x + crop_w, crop_y + crop_h)).resize(
            (target_w, target_h), Image.Resampling.LANCZOS
        )

    bg_canvas = ImageEnhance.Contrast(bg_canvas).enhance(1.14)
    bg_canvas = ImageEnhance.Color(bg_canvas).enhance(1.12)

    # Sample headline zone (left/center-left for right-subject layout) and enforce APCA Lc >= 75
    text_sample_box = (48, 120, 860, 560) if subject_on_right else (420, 120, 1232, 560)
    bg_rgb = _sample_region_avg_rgb(bg_canvas, text_sample_box)
    scrim_alpha, apca_lc = _ensure_apca_scrim_alpha(bg_rgb, fg_rgb=(248, 250, 252), min_abs_lc=75.0, base_alpha=0.58)

    lines = _format_headline_lines(final_headline, max_words=4)
    longest_line = max(len(ln) for ln in lines)
    font_px = 152 if longest_line <= 9 else (130 if longest_line <= 13 else 112)
    headline_html = _highlight_accent_word_html(lines, accent_hex="#FFD400")
    raw_badge = badge_text.strip().upper()[:14]
    # Anti-duplication: omit eyebrow badge when the headline already contains the exact badge string
    if raw_badge and raw_badge in " ".join(lines).upper():
        raw_badge = ""
    badge_html = html_lib.escape(raw_badge) if raw_badge else ""

    bg_uri = _img_to_data_uri(bg_canvas, fmt="JPEG")
    font_b64 = _get_font_base64()

    # Directional scrim & positioning mirror cleanly based on subject side (Rule of Thirds)
    grad_dir = "90deg" if subject_on_right else "270deg"
    text_pos_css = "left: 56px; align-items: flex-start; text-align: left;" if subject_on_right else "right: 56px; align-items: flex-end; text-align: right;"
    subj_pos_css = "right: -12px;" if subject_on_right else "left: -12px;"

    html_doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<style>
@font-face {{
  font-family: 'MontserratBlack';
  src: url('data:font/ttf;base64,{font_b64}') format('truetype');
  font-weight: 900;
  font-style: normal;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html, body {{
  width: 1280px;
  height: 720px;
  overflow: hidden;
  background: #080a10;
  position: relative;
  font-family: 'MontserratBlack', sans-serif;
}}
.bg {{
  position: absolute;
  inset: 0;
  width: 1280px;
  height: 720px;
  object-fit: cover;
  filter: saturate(1.12) contrast(1.06);
}}
.scrim {{
  position: absolute;
  inset: 0;
  background:
    linear-gradient({grad_dir}, rgba(6,8,14,{min(0.92, scrim_alpha + 0.14):.2f}) 0%, rgba(6,8,14,{scrim_alpha:.2f}) 52%, rgba(6,8,14,0.12) 100%),
    linear-gradient(180deg, rgba(6,8,14,0.42) 0%, rgba(6,8,14,0.0) 24%, rgba(6,8,14,0.0) 76%, rgba(6,8,14,0.56) 100%);
}}
/* Warm atmospheric backlight separating subject cutout from mid-layer text (Gurney rim-light principle) */
.backlight {{
  position: absolute;
  bottom: -40px;
  {'right: 40px;' if subject_on_right else 'left: 40px;'}
  width: 560px;
  height: 560px;
  background: radial-gradient(circle, rgba(255, 196, 28, 0.34) 0%, rgba(255, 92, 24, 0.14) 44%, rgba(0, 0, 0, 0) 72%);
  z-index: 2;
}}
/* Middle Z-index: Text-Behind-Subject monumental typography */
.headline-layer {{
  position: absolute;
  top: 50%;
  transform: translateY(-50%);
  {text_pos_css}
  width: 860px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  z-index: 3;
}}
.eyebrow {{
  display: inline-block;
  font-size: 24px;
  letter-spacing: 0.16em;
  color: #090B10;
  background: #FFD400;
  padding: 6px 18px 5px 18px;
  border-radius: 4px;
  margin-bottom: 4px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.55);
}}
.hl-line {{
  font-family: 'MontserratBlack', sans-serif;
  font-size: {font_px}px;
  line-height: 0.89;
  letter-spacing: -0.04em;
  color: #F8FAFC;
  text-transform: uppercase;
  text-shadow:
    0 12px 34px rgba(0, 0, 0, 0.94),
    0 3px 6px rgba(0, 0, 0, 0.98);
  -webkit-text-stroke: 3px rgba(6, 8, 14, 0.78);
  paint-order: stroke fill;
}}
/* Foreground Z-index: BiRefNet-lite subject cutout overlapping text edges with pictorial warm rim-light */
.subject-cutout {{
  position: absolute;
  bottom: -6px;
  {subj_pos_css}
  max-height: 610px;
  max-width: 680px;
  width: auto;
  height: auto;
  object-fit: contain;
  z-index: 5;
  filter:
    drop-shadow(0 0 2px rgba(255, 224, 102, 0.92))
    drop-shadow(-8px -4px 18px rgba(255, 184, 28, 0.42))
    drop-shadow(0 18px 34px rgba(0, 0, 0, 0.82));
}}
.grain {{
  position: absolute;
  inset: 0;
  z-index: 6;
  opacity: 0.048;
  mix-blend-mode: overlay;
  pointer-events: none;
}}
</style>
</head>
<body>
  <img class="bg" src="{bg_uri}" alt="" />
  <div class="scrim"></div>
  <div class="backlight"></div>
  <div class="headline-layer">
    {f'<div class="eyebrow">{badge_html}</div>' if badge_html else ''}
    {headline_html}
  </div>
  {f'<img class="subject-cutout" src="{cutout_uri}" alt="" />' if cutout_uri else ''}
  <svg class="grain" width="1280" height="720" xmlns="http://www.w3.org/2000/svg">
    <filter id="g"><feTurbulence type="fractalNoise" baseFrequency="0.78" numOctaves="2" stitchTiles="stitch"/></filter>
    <rect width="1280" height="720" filter="url(#g)" />
  </svg>
</body>
</html>"""

    rendered_img, renderer_used = _render_html_to_image(html_doc, target_w, target_h)
    rendered_img.save(out_jpg, format="JPEG", quality=93, subsampling=0)
    with Image.open(out_jpg) as verify_img:
        verified_size = verify_img.size
    if verified_size != (1280, 720):
        raise RuntimeError(f"Runtime PIL.Image.size check failed for 16:9 thumbnail: got {verified_size}")
    clean_tmp.unlink(missing_ok=True)

    stem = out_jpg.stem
    squint_jpg = out_jpg.with_name(f"{stem}_squint_160px.jpg")
    crop_jpg = out_jpg.with_name(f"{stem}_crop_preview.jpg")
    saliency_jpg = out_jpg.with_name(f"{stem}_saliency_heatmap.jpg")

    render_squint_test(rendered_img, squint_jpg, target_width=160)
    render_crop_preview(rendered_img, crop_jpg, fmt="16_9")
    saliency = compute_spectral_saliency(rendered_img, safe_box=SAFE_BOX_16_9, out_heatmap_jpg=saliency_jpg, apca_lc=apca_lc)

    return {
        "format": "16_9",
        "thumbnail_path": str(out_jpg),
        "cover_timestamp_ms": layer1["cover_timestamp_ms"],
        "cover_headline_used": final_headline,
        "title_overlap_words": overlap_words,
        "dimensions": list(verified_size),
        "renderer": renderer_used,
        "font": "Montserrat-Black.ttf (embedded base64)",
        "apca_lc": apca_lc,
        "scrim_alpha": scrim_alpha,
        "matting": matting_meta,
        "rag_design_hits": rag_hits,
        "saliency": saliency,
        "qa_artifacts": {
            "squint_160px": str(squint_jpg),
            "crop_preview": str(crop_jpg),
            "saliency_heatmap": str(saliency_jpg),
        },
    }
