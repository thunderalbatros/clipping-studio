"""Pure ONNX UltraFace + LR-ASD Active Speaker Detection restricted strictly to streamer webcam crop (Hito 2.0).

Re-implements the MIT reference pipeline (UltraFace RFB-320 + LR-ASD frontend/backend + exact 13-dim MFCC)
in pure NumPy + onnxruntime==1.32.0 + Pillow + FFmpeg with zero TensorFlow/OpenCV/SciPy dependencies.
"""

from __future__ import annotations

import math
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Any

import numpy as np
import onnxruntime as ort
from PIL import Image

from src.schemas import WordTimestamp

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = PROJECT_ROOT / "models"

ULTRAFACE_W = 320
ULTRAFACE_H = 240
ASD_FPS = 25
ASD_CROP_SIZE = 112
ASD_CROP_SIDE_FACTOR = 0.7
ASD_CROP_DOWN_SHIFT = 0.2
ASD_CROP_PAD = 110
FRONTEND_BATCH_FRAMES = 25
FRONTEND_CONTEXT_FRAMES = 9
BACKEND_WINDOWS_SEC = (1, 2, 3, 4, 5, 6)

_SESSIONS: dict[str, ort.InferenceSession] = {}


def _get_session(model_name: str) -> ort.InferenceSession:
    if model_name not in _SESSIONS:
        path = MODELS_DIR / model_name
        if not path.exists():
            raise FileNotFoundError(f"ONNX model not found at {path}")
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        _SESSIONS[model_name] = ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])
    return _SESSIONS[model_name]


def _iou(a: tuple[float, float, float, float, float], b: tuple[float, float, float, float, float]) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    return inter / max(1e-6, area_a + area_b - inter)


def detect_faces_ultraface(rgb_frame: np.ndarray, confidence: float = 0.65) -> list[tuple[float, float, float, float, float]]:
    """Run UltraFace RFB-320 on an RGB uint8 HxWx3 frame; returns [(x1, y1, x2, y2, score)] normalized 0..1."""
    if rgb_frame.shape[0] != ULTRAFACE_H or rgb_frame.shape[1] != ULTRAFACE_W:
        img = Image.fromarray(rgb_frame, mode="RGB").resize((ULTRAFACE_W, ULTRAFACE_H), Image.Resampling.BILINEAR)
        arr = np.asarray(img, dtype=np.float32)
    else:
        arr = rgb_frame.astype(np.float32)

    inp = ((arr - 127.0) / 128.0).transpose(2, 0, 1)[None, :, :, :]
    sess = _get_session("ultraface-rfb-320.onnx")
    scores_out, boxes_out = sess.run(None, {"input": inp})
    scores = scores_out[0, :, 1]
    boxes = boxes_out[0]
    idxs = np.where(scores >= confidence)[0]
    if len(idxs) == 0:
        return []

    cands = [
        (float(boxes[i, 0]), float(boxes[i, 1]), float(boxes[i, 2]), float(boxes[i, 3]), float(scores[i]))
        for i in idxs
    ]
    cands.sort(key=lambda b: b[4], reverse=True)
    kept: list[tuple[float, float, float, float, float]] = []
    for box in cands:
        if all(_iou(k, box) < 0.5 for k in kept):
            kept.append(box)
    return kept


def compute_mfcc_lr_asd(pcm_int16: np.ndarray, sample_rate: int = 16000) -> np.ndarray:
    """Bit-accurate NumPy MFCC matching python_speech_features defaults (13 cepstra, 26 mel, 512 FFT, 25ms/10ms)."""
    nfft = 512
    nfilt = 26
    numcep = 13
    preemph = 0.97
    ceplifter = 22
    frame_len = round(0.025 * sample_rate)
    frame_step = round(0.010 * sample_rate)
    eps = np.finfo(np.float64).eps

    pcm_f = pcm_int16.astype(np.float64)
    if len(pcm_f) == 0:
        return np.zeros((1, numcep), dtype=np.float32)
    sig = np.empty_like(pcm_f)
    sig[0] = pcm_f[0]
    if len(pcm_f) > 1:
        sig[1:] = pcm_f[1:] - preemph * pcm_f[:-1]

    slen = len(sig)
    n_frames = 1 if slen <= frame_len else 1 + math.ceil((slen - frame_len) / frame_step)
    pad_len = (n_frames - 1) * frame_step + frame_len
    if pad_len > slen:
        sig = np.pad(sig, (0, pad_len - slen), mode="constant")

    indices = np.arange(frame_len)[None, :] + np.arange(n_frames)[:, None] * frame_step
    frames = sig[indices]  # rectangular window

    spec = np.fft.rfft(frames, n=nfft, axis=1)
    pspec = (spec.real**2 + spec.imag**2) / float(nfft)
    energy = np.maximum(np.sum(pspec, axis=1), eps)

    low_mel = 0.0
    high_mel = 2595.0 * math.log10(1.0 + (sample_rate / 2.0) / 700.0)
    mel_pts = np.linspace(low_mel, high_mel, nfilt + 2)
    hz_pts = 700.0 * (10.0 ** (mel_pts / 2595.0) - 1.0)
    bins = np.floor((nfft + 1) * hz_pts / float(sample_rate)).astype(np.int64)

    fbank = np.zeros((nfilt, nfft // 2 + 1), dtype=np.float64)
    for j in range(nfilt):
        b0, b1, b2 = int(bins[j]), int(bins[j + 1]), int(bins[j + 2])
        if b1 > b0:
            for i in range(b0, b1):
                fbank[j, i] = (i - b0) / float(b1 - b0)
        if b2 > b1:
            for i in range(b1, b2):
                fbank[j, i] = (b2 - i) / float(b2 - b1)

    feat_mel = np.log(np.maximum(pspec @ fbank.T, eps))

    # Orthonormal DCT-II matrix [numcep, nfilt]
    k_idx = np.arange(numcep)[:, None]
    n_idx = np.arange(nfilt)[None, :]
    scale = np.where(k_idx == 0, math.sqrt(1.0 / (4.0 * nfilt)), math.sqrt(1.0 / (2.0 * nfilt)))
    dct = 2.0 * scale * np.cos(math.pi * k_idx * (2.0 * n_idx + 1.0) / (2.0 * nfilt))
    mfcc = feat_mel @ dct.T

    lifter = 1.0 + (ceplifter / 2.0) * np.sin(math.pi * np.arange(numcep) / float(ceplifter))
    mfcc *= lifter[None, :]
    mfcc[:, 0] = np.log(energy)
    return mfcc.astype(np.float32)


def _crop_mouth_patch_bilinear(gray_frame: np.ndarray, cx: float, cy: float, side: float) -> np.ndarray:
    """Bilinear sample of a mouth-centered square into 112x112 float32 (0..255) matching LR-ASD preprocessing."""
    fh, fw = gray_frame.shape
    x0 = cx - side / 2.0
    y0 = cy - side / 2.0
    step = side / float(ASD_CROP_SIZE)

    xs = x0 + (np.arange(ASD_CROP_SIZE, dtype=np.float32) + 0.5) * step - 0.5
    ys = y0 + (np.arange(ASD_CROP_SIZE, dtype=np.float32) + 0.5) * step - 0.5
    gx, gy = np.meshgrid(xs, ys)

    ix = np.floor(gx).astype(np.int32)
    iy = np.floor(gy).astype(np.int32)
    fx = gx - ix
    fy = gy - iy

    def _sample(px: np.ndarray, py: np.ndarray) -> np.ndarray:
        valid = (px >= 0) & (py >= 0) & (px < fw) & (py < fh)
        safe_x = np.clip(px, 0, fw - 1)
        safe_y = np.clip(py, 0, fh - 1)
        vals = gray_frame[safe_y, safe_x].astype(np.float32)
        return np.where(valid, vals, float(ASD_CROP_PAD))

    v00 = _sample(ix, iy)
    v10 = _sample(ix + 1, iy)
    v01 = _sample(ix, iy + 1)
    v11 = _sample(ix + 1, iy + 1)
    patch = v00 * (1.0 - fx) * (1.0 - fy) + v10 * fx * (1.0 - fy) + v01 * (1.0 - fx) * fy + v11 * fx * fy
    return np.floor(patch).astype(np.float32)


def _extract_webcam_frames_rgb(video_path: Path, webcam_box_px: tuple[int, int, int, int], fps: int = ASD_FPS) -> tuple[np.ndarray, int, int]:
    """Decode cropped streamer webcam stream (downscaled <=240px wide to cap RAM) as uint8 RGB array [N, H, W, 3]."""
    wx, wy, ww, wh = webcam_box_px
    ww = max(32, ww & ~1)
    wh = max(32, wh & ~1)
    max_w = 240
    if ww > max_w:
        out_w = max_w
        out_h = max(32, round(wh * (max_w / float(ww))) & ~1)
        vf = f"crop={ww}:{wh}:{wx}:{wy},scale={out_w}:{out_h},fps={fps}"
    else:
        out_w, out_h = ww, wh
        vf = f"crop={ww}:{wh}:{wx}:{wy},fps={fps}"
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video_path),
            "-vf",
            vf,
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        capture_output=True,
        check=True,
    )
    frame_bytes = out_w * out_h * 3
    n_frames = len(proc.stdout) // frame_bytes
    if n_frames == 0:
        return np.zeros((0, out_h, out_w, 3), dtype=np.uint8), out_w, out_h
    arr = np.frombuffer(proc.stdout[: n_frames * frame_bytes], dtype=np.uint8).reshape((n_frames, out_h, out_w, 3))
    return arr, out_w, out_h


def _extract_pcm_16k(video_path: Path) -> np.ndarray:
    with tempfile.TemporaryDirectory(prefix="cs_asd_wav_") as tmp:
        wav_path = Path(tmp) / "audio.wav"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(video_path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(wav_path),
            ],
            check=True,
        )
        with wave.open(str(wav_path), "rb") as wf:
            raw = wf.readframes(wf.getnframes())
    return np.frombuffer(raw, dtype=np.int16)


def detect_face_center_ultraface(
    video_path: Path,
    webcam_box_px: tuple[int, int, int, int],
) -> tuple[float, float, float, float] | None:
    """Detect median streamer face box (x1, y1, x2, y2 normalized inside webcam_box_px) using UltraFace ONNX."""
    frames, _, _ = _extract_webcam_frames_rgb(video_path, webcam_box_px, fps=2)
    if len(frames) == 0:
        return None
    step = max(1, len(frames) // 20)
    boxes: list[tuple[float, float, float, float]] = []
    for idx in range(0, len(frames), step):
        found = detect_faces_ultraface(frames[idx], confidence=0.55)
        if found:
            best = max(found, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
            boxes.append((best[0], best[1], best[2], best[3]))
    if not boxes:
        return None
    arr = np.array(boxes, dtype=np.float32)
    med = np.median(arr, axis=0)
    return (float(med[0]), float(med[1]), float(med[2]), float(med[3]))


def _run_lr_asd_scores(mouth_crops: np.ndarray, mfcc: np.ndarray) -> np.ndarray:
    """Execute LR-ASD frontend (batched 25 frames + context 9) and multi-window backend ensemble."""
    n_frames = len(mouth_crops)
    if n_frames < 2:
        return np.zeros(n_frames, dtype=np.float32)

    need_audio = n_frames * 4
    if len(mfcc) < need_audio:
        pad_rows = np.repeat(mfcc[-1:], need_audio - len(mfcc), axis=0)
        mfcc_padded = np.vstack([mfcc, pad_rows])
    else:
        mfcc_padded = mfcc[:need_audio]

    fe_sess = _get_session("lr-asd-frontend.onnx")
    be_sess = _get_session("lr-asd-backend.onnx")

    embed_a = np.zeros((n_frames, 128), dtype=np.float32)
    embed_v = np.zeros((n_frames, 128), dtype=np.float32)

    for start in range(0, n_frames, FRONTEND_BATCH_FRAMES):
        end = min(n_frames, start + FRONTEND_BATCH_FRAMES)
        in_start = max(0, start - FRONTEND_CONTEXT_FRAMES)
        in_end = min(n_frames, end + FRONTEND_CONTEXT_FRAMES)
        vid_win = mouth_crops[in_start:in_end][None, :, :, :]  # [1, tv, 112, 112]
        aud_win = mfcc_padded[in_start * 4 : in_end * 4][None, :, :]  # [1, tv*4, 13]
        out_a, out_v = fe_sess.run(None, {"audio": aud_win, "video": vid_win})
        off = start - in_start
        length = end - start
        embed_a[start:end] = out_a[0, off : off + length]
        embed_v[start:end] = out_v[0, off : off + length]

    score_sum = np.zeros(n_frames, dtype=np.float32)
    passes = 0
    for sec in BACKEND_WINDOWS_SEC:
        win = sec * ASD_FPS
        for start in range(0, n_frames, win):
            end = min(n_frames, start + win)
            scores_av, _ = be_sess.run(
                None,
                {
                    "embedA": embed_a[start:end][None, :, :],
                    "embedV": embed_v[start:end][None, :, :],
                },
            )
            score_sum[start:end] += np.asarray(scores_av, dtype=np.float32).reshape(-1)
        passes += 1
        if win >= n_frames:
            break

    avg = score_sum / float(max(1, passes))
    # Smooth over +/-2 frames matching reference implementation
    smoothed = np.empty_like(avg)
    for i in range(n_frames):
        smoothed[i] = float(np.mean(avg[max(0, i - 2) : min(n_frames, i + 3)]))
    return smoothed


def score_streamer_speech_asd(
    video_path: Path,
    webcam_box_px: tuple[int, int, int, int] | None,
    words: list[WordTimestamp],
    logit_threshold: float = -0.65,
) -> dict[str, Any]:
    """Run UltraFace + LR-ASD strictly inside webcam_box_px and label NPC/silent words as is_npc_or_other=True."""
    if not webcam_box_px or not words:
        return {"ran": False, "reason": "no_webcam_or_words", "face_detected_ratio": 0.0, "npc_words_marked": 0}

    frames_rgb, ww, wh = _extract_webcam_frames_rgb(video_path, webcam_box_px, fps=ASD_FPS)
    n_frames = len(frames_rgb)
    if n_frames < 5:
        return {"ran": False, "reason": "too_few_frames", "face_detected_ratio": 0.0, "npc_words_marked": 0}

    # Detect streamer face inside webcam every 5 frames (5 fps) via UltraFace
    stride = 5
    raw_boxes: list[tuple[float, float, float, float] | None] = [None] * n_frames
    detected_cnt = 0
    for f in range(0, n_frames, stride):
        found = detect_faces_ultraface(frames_rgb[f], confidence=0.50)
        if found:
            best = max(found, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
            raw_boxes[f] = (best[0], best[1], best[2], best[3])
            detected_cnt += 1

    valid_boxes = [b for b in raw_boxes if b is not None]
    sample_total = max(1, (n_frames + stride - 1) // stride)
    face_ratio = round(detected_cnt / float(sample_total), 3)
    if not valid_boxes or face_ratio < 0.15:
        return {"ran": False, "reason": "no_streamer_face_in_webcam", "face_detected_ratio": face_ratio, "npc_words_marked": 0}

    # Fill missing frames with nearest/last known box + median smooth over 13 frames (~0.5s)
    med_default = tuple(float(v) for v in np.median(np.array(valid_boxes, dtype=np.float32), axis=0))
    filled = np.zeros((n_frames, 4), dtype=np.float32)
    last_b = med_default
    for f in range(n_frames):
        if raw_boxes[f] is not None:
            last_b = raw_boxes[f]  # type: ignore[assignment]
        filled[f] = last_b

    smooth_boxes = np.empty_like(filled)
    for f in range(n_frames):
        smooth_boxes[f] = np.median(filled[max(0, f - 6) : min(n_frames, f + 7)], axis=0)

    # Convert RGB to grayscale uint8 and crop 112x112 mouth-centered squares
    gray_all = (
        0.299 * frames_rgb[:, :, :, 0] + 0.587 * frames_rgb[:, :, :, 1] + 0.114 * frames_rgb[:, :, :, 2]
    ).astype(np.uint8)
    mouth_crops = np.empty((n_frames, ASD_CROP_SIZE, ASD_CROP_SIZE), dtype=np.float32)
    for f in range(n_frames):
        x1, y1, x2, y2 = smooth_boxes[f]
        bw = (x2 - x1) * ww
        bh = (y2 - y1) * wh
        size = max(bw, bh)
        cx = ((x1 + x2) * 0.5) * ww
        cy = ((y1 + y2) * 0.5) * wh + ASD_CROP_DOWN_SHIFT * size
        mouth_crops[f] = _crop_mouth_patch_bilinear(gray_all[f], cx, cy, max(4.0, size * ASD_CROP_SIDE_FACTOR))

    pcm = _extract_pcm_16k(video_path)
    mfcc = compute_mfcc_lr_asd(pcm, sample_rate=16000)
    frame_logits = _run_lr_asd_scores(mouth_crops, mfcc)

    # Assign per-word LR-ASD score (+/- 3 frames co-articulation margin = +/- 120ms)
    npc_marked = 0
    for w in words:
        f0 = max(0, round(w.start * ASD_FPS) - 3)
        f1 = min(n_frames, max(f0 + 1, math.ceil(w.end * ASD_FPS) + 3))
        if f0 >= n_frames:
            w.streamer_asd_score = 0.0
            continue
        seg_scores = frame_logits[f0:f1]
        # Use 75th percentile logit across word span so short plosives / brief lip closures don't drop real speech
        word_score = round(float(np.percentile(seg_scores, 75)), 3)
        w.streamer_asd_score = word_score
        if word_score < logit_threshold:
            w.is_npc_or_other = True
            npc_marked += 1
        else:
            w.is_npc_or_other = False

    med_face = tuple(round(float(v), 4) for v in np.median(smooth_boxes, axis=0))
    return {
        "ran": True,
        "face_detected_ratio": face_ratio,
        "median_face_box_norm": med_face,
        "logit_threshold": logit_threshold,
        "total_words": len(words),
        "npc_words_marked": npc_marked,
    }
