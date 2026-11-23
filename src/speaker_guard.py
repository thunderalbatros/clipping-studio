"""Pure ONNX UltraFace + LR-ASD Active Speaker Detection restricted strictly to streamer webcam crop (Hito 2.0).

Re-implements the MIT reference pipeline (UltraFace RFB-320 + LR-ASD frontend/backend + exact 13-dim MFCC)
in pure NumPy + onnxruntime==1.32.0 + Pillow + FFmpeg with zero TensorFlow/OpenCV/SciPy dependencies.
"""

from __future__ import annotations

import math
import subprocess
from collections import deque
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import onnxruntime as ort
from PIL import Image

from src.resource_guard import onnx_threads
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
        opts.intra_op_num_threads = onnx_threads()
        opts.inter_op_num_threads = 1
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
    outputs = sess.run(None, {"input": inp})
    scores_arr = np.asarray(outputs[0], dtype=np.float32)
    boxes_arr = np.asarray(outputs[1], dtype=np.float32)
    scores = scores_arr[0, :, 1]
    boxes = boxes_arr[0]
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

    # Orthonormal DCT-II matrix [numcep, nfilt]
    k_idx = np.arange(numcep)[:, None]
    n_idx = np.arange(nfilt)[None, :]
    scale = np.where(k_idx == 0, math.sqrt(1.0 / (4.0 * nfilt)), math.sqrt(1.0 / (2.0 * nfilt)))
    dct = 2.0 * scale * np.cos(math.pi * k_idx * (2.0 * n_idx + 1.0) / (2.0 * nfilt))
    lifter = 1.0 + (ceplifter / 2.0) * np.sin(math.pi * np.arange(numcep) / float(ceplifter))

    # Zero-copy strided frame view + bounded 1000-frame FFT chunks (~4 MB peak instead of ~135 MB)
    frames_view = np.lib.stride_tricks.sliding_window_view(sig, frame_len)[::frame_step][:n_frames]
    out_mfcc = np.empty((n_frames, numcep), dtype=np.float32)
    chunk_sz = 1000
    fbank_t = fbank.T
    dct_t = dct.T
    for start in range(0, n_frames, chunk_sz):
        end = min(n_frames, start + chunk_sz)
        spec = np.fft.rfft(frames_view[start:end], n=nfft, axis=1)
        pspec = (spec.real**2 + spec.imag**2) / float(nfft)
        energy = np.maximum(np.sum(pspec, axis=1), eps)
        feat_mel = np.log(np.maximum(pspec @ fbank_t, eps))
        chunk_mfcc = feat_mel @ dct_t
        chunk_mfcc *= lifter[None, :]
        chunk_mfcc[:, 0] = np.log(energy)
        out_mfcc[start:end] = chunk_mfcc.astype(np.float32)
    return out_mfcc


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


def _webcam_vf_and_dims(webcam_box_px: tuple[int, int, int, int], fps: int) -> tuple[str, int, int]:
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
    return vf, out_w, out_h


def _read_exact(stream: Any, n_bytes: int) -> bytes:
    """Read exactly `n_bytes` from a binary pipe (handling partial reads) until EOF."""
    buf = bytearray()
    while len(buf) < n_bytes:
        chunk = stream.read(n_bytes - len(buf))
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def _iter_webcam_frames(
    video_path: Path,
    webcam_box_px: tuple[int, int, int, int],
    *,
    fps: int = ASD_FPS,
    pix_fmt: str = "rgb24",
) -> tuple[Iterator[np.ndarray], int, int]:
    """Stream cropped webcam frames one at a time via FFmpeg stdout pipe (bounded <1 MB RAM)."""
    vf, out_w, out_h = _webcam_vf_and_dims(webcam_box_px, fps)
    channels = 3 if pix_fmt == "rgb24" else 1
    frame_bytes = out_w * out_h * channels

    def _gen() -> Iterator[np.ndarray]:
        proc = subprocess.Popen(
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
                pix_fmt,
                "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert proc.stdout is not None
        try:
            while True:
                raw = _read_exact(proc.stdout, frame_bytes)
                if len(raw) < frame_bytes:
                    break
                if channels == 3:
                    yield np.frombuffer(raw, dtype=np.uint8).reshape((out_h, out_w, 3))
                else:
                    yield np.frombuffer(raw, dtype=np.uint8).reshape((out_h, out_w))
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            else:
                proc.wait(timeout=5)

    return _gen(), out_w, out_h


def _extract_pcm_16k(video_path: Path) -> np.ndarray:
    """Decode mono 16kHz s16le audio directly from FFmpeg pipe (zero temp WAV files on disk)."""
    proc = subprocess.run(
        [
            "ffmpeg",
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
            "-f",
            "s16le",
            "-c:a",
            "pcm_s16le",
            "-",
        ],
        capture_output=True,
        check=True,
        timeout=90,
    )
    n_samples = len(proc.stdout) // 2
    if n_samples == 0:
        return np.zeros(0, dtype=np.int16)
    return np.frombuffer(proc.stdout[: n_samples * 2], dtype=np.int16).copy()


def detect_face_center_ultraface(
    video_path: Path,
    webcam_box_px: tuple[int, int, int, int],
) -> tuple[float, float, float, float] | None:
    """Detect median streamer face box (x1, y1, x2, y2 normalized inside webcam_box_px) using streaming UltraFace ONNX."""
    frame_iter, _, _ = _iter_webcam_frames(video_path, webcam_box_px, fps=1, pix_fmt="rgb24")
    boxes: list[tuple[float, float, float, float]] = []
    for idx, frame in enumerate(frame_iter):
        if idx >= 40 and idx % 2 != 0:
            continue
        found = detect_faces_ultraface(frame, confidence=0.55)
        if found:
            best = max(found, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
            boxes.append((best[0], best[1], best[2], best[3]))
    if not boxes:
        return None
    arr = np.array(boxes, dtype=np.float32)
    med = np.median(arr, axis=0)
    return (float(med[0]), float(med[1]), float(med[2]), float(med[3]))


def _run_lr_asd_backend_scores(embed_a: np.ndarray, embed_v: np.ndarray) -> np.ndarray:
    """Run multi-window LR-ASD backend ensemble over precomputed [N, 128] audio/visual embeddings."""
    n_frames = len(embed_a)
    if n_frames < 2:
        return np.zeros(n_frames, dtype=np.float32)

    be_sess = _get_session("lr-asd-backend.onnx")
    score_sum = np.zeros(n_frames, dtype=np.float32)
    passes = 0
    for sec in BACKEND_WINDOWS_SEC:
        win = sec * ASD_FPS
        for start in range(0, n_frames, win):
            end = min(n_frames, start + win)
            be_out = be_sess.run(
                None,
                {
                    "embedA": embed_a[start:end][None, :, :],
                    "embedV": embed_v[start:end][None, :, :],
                },
            )
            score_sum[start:end] += np.asarray(be_out[0], dtype=np.float32).reshape(-1)
        passes += 1
        if win >= n_frames:
            break

    avg = score_sum / float(max(1, passes))
    smoothed = np.empty_like(avg)
    for i in range(n_frames):
        smoothed[i] = float(np.mean(avg[max(0, i - 2) : min(n_frames, i + 3)]))
    return smoothed


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
    embed_a = np.zeros((n_frames, 128), dtype=np.float32)
    embed_v = np.zeros((n_frames, 128), dtype=np.float32)

    for start in range(0, n_frames, FRONTEND_BATCH_FRAMES):
        end = min(n_frames, start + FRONTEND_BATCH_FRAMES)
        in_start = max(0, start - FRONTEND_CONTEXT_FRAMES)
        in_end = min(n_frames, end + FRONTEND_CONTEXT_FRAMES)
        vid_win = mouth_crops[in_start:in_end][None, :, :, :]  # [1, tv, 112, 112]
        aud_win = mfcc_padded[in_start * 4 : in_end * 4][None, :, :]  # [1, tv*4, 13]
        fe_out = fe_sess.run(None, {"audio": aud_win, "video": vid_win})
        out_a = np.asarray(fe_out[0], dtype=np.float32)
        out_v = np.asarray(fe_out[1], dtype=np.float32)
        off = start - in_start
        length = end - start
        embed_a[start:end] = out_a[0, off : off + length]
        embed_v[start:end] = out_v[0, off : off + length]

    return _run_lr_asd_backend_scores(embed_a, embed_v)


def score_streamer_speech_asd(
    video_path: Path,
    webcam_box_px: tuple[int, int, int, int] | None,
    words: list[WordTimestamp],
    logit_threshold: float = -0.65,
) -> dict[str, Any]:
    """Run streaming UltraFace + chunked LR-ASD strictly inside webcam_box_px with bounded low RAM (<25 MB)."""
    if not webcam_box_px or not words:
        return {"ran": False, "reason": "no_webcam_or_words", "face_detected_ratio": 0.0, "npc_words_marked": 0}

    # Pass A: Stream rgb24 at 5fps (stride=5 of 25fps) for UltraFace — holds ONLY 1 frame (~130 KB) in RAM!
    stride = 5
    detect_fps = max(1, ASD_FPS // stride)
    rgb_iter, ww, wh = _iter_webcam_frames(video_path, webcam_box_px, fps=detect_fps, pix_fmt="rgb24")
    sampled_boxes: list[tuple[float, float, float, float] | None] = []
    detected_cnt = 0
    for frame in rgb_iter:
        found = detect_faces_ultraface(frame, confidence=0.50)
        if found:
            best = max(found, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
            sampled_boxes.append((best[0], best[1], best[2], best[3]))
            detected_cnt += 1
        else:
            sampled_boxes.append(None)

    sample_total = len(sampled_boxes)
    if sample_total == 0:
        return {"ran": False, "reason": "too_few_frames", "face_detected_ratio": 0.0, "npc_words_marked": 0}

    valid_boxes = [b for b in sampled_boxes if b is not None]
    face_ratio = round(detected_cnt / float(sample_total), 3)
    if not valid_boxes or face_ratio < 0.15:
        return {"ran": False, "reason": "no_streamer_face_in_webcam", "face_detected_ratio": face_ratio, "npc_words_marked": 0}

    # Build smoothed 5fps track then expand to 25fps on the fly during Pass B streaming
    med_default = tuple(float(v) for v in np.median(np.array(valid_boxes, dtype=np.float32), axis=0))
    filled_5fps = np.zeros((sample_total, 4), dtype=np.float32)
    last_b = med_default
    for idx, sb in enumerate(sampled_boxes):
        if sb is not None:
            last_b = sb
        filled_5fps[idx] = last_b

    smooth_5fps = np.empty_like(filled_5fps)
    for idx in range(sample_total):
        smooth_5fps[idx] = np.median(filled_5fps[max(0, idx - 1) : min(sample_total, idx + 2)], axis=0)

    # Extract audio MFCC once (compact float32, ~2-4 MB for 90s slice)
    pcm = _extract_pcm_16k(video_path)
    mfcc = compute_mfcc_lr_asd(pcm, sample_rate=16000)
    del pcm

    # Pass B: Stream single-channel uint8 gray frames at 25fps directly from FFmpeg into chunked LR-ASD frontend
    # Rolling buffer holds at most FRONTEND_BATCH_FRAMES + 2*FRONTEND_CONTEXT_FRAMES (~43 crops = ~2.1 MB RAM)
    fe_sess = _get_session("lr-asd-frontend.onnx")
    gray_iter, ww, wh = _iter_webcam_frames(video_path, webcam_box_px, fps=ASD_FPS, pix_fmt="gray")

    embed_a_chunks: list[np.ndarray] = []
    embed_v_chunks: list[np.ndarray] = []
    ring_crops: deque[tuple[int, np.ndarray]] = deque()
    next_batch_start = 0
    n_frames = 0

    def _get_mfcc_window(f_start: int, f_end: int) -> np.ndarray:
        a0, a1 = f_start * 4, f_end * 4
        need = a1 - a0
        if len(mfcc) == 0:
            return np.zeros((need, 13), dtype=np.float32)
        if a1 <= len(mfcc):
            return mfcc[a0:a1]
        if a0 >= len(mfcc):
            return np.repeat(mfcc[-1:], need, axis=0)
        head = mfcc[a0:]
        pad = np.repeat(mfcc[-1:], need - len(head), axis=0)
        return np.vstack([head, pad])

    def _flush_batch(batch_start: int, batch_end: int) -> None:
        in_start = max(0, batch_start - FRONTEND_CONTEXT_FRAMES)
        in_end = batch_end + FRONTEND_CONTEXT_FRAMES
        window_items = [crop for idx, crop in ring_crops if in_start <= idx < in_end]
        if not window_items:
            return
        actual_start = next(idx for idx, _ in ring_crops if in_start <= idx < in_end)
        actual_end = actual_start + len(window_items)
        vid_win = np.stack(window_items, axis=0)[None, :, :, :]  # [1, tv, 112, 112]
        aud_win = _get_mfcc_window(actual_start, actual_end)[None, :, :]  # [1, tv*4, 13]
        fe_out = fe_sess.run(None, {"audio": aud_win, "video": vid_win})
        out_a = np.asarray(fe_out[0], dtype=np.float32)
        out_v = np.asarray(fe_out[1], dtype=np.float32)
        off = batch_start - actual_start
        length = batch_end - batch_start
        embed_a_chunks.append(out_a[0, off : off + length].copy())
        embed_v_chunks.append(out_v[0, off : off + length].copy())
        # Evict crops no longer needed for future left-context windows
        min_keep = max(0, batch_end - FRONTEND_CONTEXT_FRAMES)
        while ring_crops and ring_crops[0][0] < min_keep:
            ring_crops.popleft()

    for f_idx, gray_frame in enumerate(gray_iter):
        s_idx = min(sample_total - 1, f_idx // stride)
        x1, y1, x2, y2 = smooth_5fps[s_idx]
        bw = (x2 - x1) * ww
        bh = (y2 - y1) * wh
        size = max(bw, bh)
        cx = ((x1 + x2) * 0.5) * ww
        cy = ((y1 + y2) * 0.5) * wh + ASD_CROP_DOWN_SHIFT * size
        crop = _crop_mouth_patch_bilinear(gray_frame, cx, cy, max(4.0, size * ASD_CROP_SIDE_FACTOR))
        ring_crops.append((f_idx, crop))
        n_frames = f_idx + 1

        # Once we have enough right-context frames after a full batch, run frontend & free old crops
        if n_frames >= next_batch_start + FRONTEND_BATCH_FRAMES + FRONTEND_CONTEXT_FRAMES:
            batch_end = next_batch_start + FRONTEND_BATCH_FRAMES
            _flush_batch(next_batch_start, batch_end)
            next_batch_start = batch_end

    # Flush any remaining tail batches
    while next_batch_start < n_frames:
        batch_end = min(n_frames, next_batch_start + FRONTEND_BATCH_FRAMES)
        _flush_batch(next_batch_start, batch_end)
        next_batch_start = batch_end
    ring_crops.clear()

    if n_frames < 5 or not embed_a_chunks:
        return {"ran": False, "reason": "too_few_frames", "face_detected_ratio": face_ratio, "npc_words_marked": 0}

    embed_a = np.concatenate(embed_a_chunks, axis=0)
    embed_v = np.concatenate(embed_v_chunks, axis=0)
    embed_a_chunks.clear()
    embed_v_chunks.clear()
    frame_logits = _run_lr_asd_backend_scores(embed_a, embed_v)
    del embed_a, embed_v

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

    med_face = tuple(round(float(v), 4) for v in np.median(smooth_5fps, axis=0))
    return {
        "ran": True,
        "face_detected_ratio": face_ratio,
        "median_face_box_norm": med_face,
        "logit_threshold": logit_threshold,
        "total_words": len(words),
        "npc_words_marked": npc_marked,
    }
