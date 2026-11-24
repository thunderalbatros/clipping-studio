"""Comprehensive unit tests for `src/speaker_guard.py` (UltraFace + LR-ASD + MFCC + streaming pipes)."""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import src.speaker_guard as sg
from src.schemas import WordTimestamp


class _FakeUltraFaceSession:
    def run(self, _out_names: Any, inputs: dict[str, np.ndarray]) -> list[np.ndarray]:
        assert inputs["input"].shape == (1, 3, sg.ULTRAFACE_H, sg.ULTRAFACE_W)
        # 3 anchors: 2 overlapping high-score boxes (NMS keeps highest) + 1 low-score box
        scores = np.array([[[0.05, 0.92], [0.08, 0.85], [0.90, 0.20]]], dtype=np.float32)
        boxes = np.array(
            [
                [
                    [0.20, 0.15, 0.70, 0.80],
                    [0.22, 0.16, 0.71, 0.81],
                    [0.01, 0.01, 0.15, 0.15],
                ]
            ],
            dtype=np.float32,
        )
        return [scores, boxes]


class _FakeFrontendSession:
    def run(self, _out_names: Any, inputs: dict[str, np.ndarray]) -> list[np.ndarray]:
        tv = inputs["video"].shape[1]
        assert inputs["audio"].shape == (1, tv * 4, 13)
        emb_a = np.ones((1, tv, 128), dtype=np.float32) * 0.5
        emb_v = np.ones((1, tv, 128), dtype=np.float32) * 0.5
        return [emb_a, emb_v]


class _FakeBackendSession:
    def run(self, _out_names: Any, inputs: dict[str, np.ndarray]) -> list[np.ndarray]:
        n = inputs["embedA"].shape[1]
        # First half positive logits (streamer speaking), second half negative logits (NPC speaking)
        logits = np.array([1.8 if i < max(1, n // 2) else -2.4 for i in range(n)], dtype=np.float32)
        return [logits[None, :]]


def test_iou_mfcc_bilinear_crop_and_stream_helpers() -> None:
    box_a = (0.1, 0.1, 0.5, 0.5, 0.9)
    box_b = (0.1, 0.1, 0.5, 0.5, 0.8)
    box_c = (0.6, 0.6, 0.9, 0.9, 0.7)
    assert sg._iou(box_a, box_b) == pytest.approx(1.0, rel=1e-4)
    assert sg._iou(box_a, box_c) == 0.0

    # Empty, short, and multi-chunk (>1000 frames = >10s) MFCC paths
    empty_mfcc = sg.compute_mfcc_lr_asd(np.zeros(0, dtype=np.int16))
    assert empty_mfcc.shape == (1, 13)
    short_mfcc = sg.compute_mfcc_lr_asd(np.ones(120, dtype=np.int16) * 500)
    assert short_mfcc.shape == (1, 13)
    long_pcm = (np.sin(np.linspace(0.0, 400.0, 16000 * 12)) * 9000).astype(np.int16)
    long_mfcc = sg.compute_mfcc_lr_asd(long_pcm, sample_rate=16000)
    assert long_mfcc.shape[0] > 1050
    assert long_mfcc.shape[1] == 13
    assert np.isfinite(long_mfcc).all()

    # Bilinear mouth crop inside frame and out-of-bounds pad (110.0)
    gray = np.full((160, 160), 200, dtype=np.uint8)
    in_patch = sg._crop_mouth_patch_bilinear(gray, 80.0, 80.0, 64.0)
    assert in_patch.shape == (sg.ASD_CROP_SIZE, sg.ASD_CROP_SIZE)
    assert float(in_patch.mean()) == pytest.approx(200.0, abs=1.0)

    oob_patch = sg._crop_mouth_patch_bilinear(gray, -100.0, -100.0, 40.0)
    assert float(oob_patch[0, 0]) == pytest.approx(float(sg.ASD_CROP_PAD), abs=1e-3)

    # Webcam filter string & dimension scaling
    vf_big, w_big, h_big = sg._webcam_vf_and_dims((10, 20, 480, 270), fps=25)
    assert w_big == 240
    assert h_big % 2 == 0
    assert "scale=240:" in vf_big
    vf_small, w_sm, h_sm = sg._webcam_vf_and_dims((0, 0, 200, 150), fps=5)
    assert (w_sm, h_sm) == (200, 150)
    assert "scale=" not in vf_small

    # Partial read stream helper
    buf = io.BytesIO(b"abcdefghij")
    assert sg._read_exact(buf, 6) == b"abcdef"
    assert sg._read_exact(buf, 10) == b"ghij"


def test_ultraface_and_lr_asd_scoring_pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_sessions: dict[str, Any] = {
        "ultraface-rfb-320.onnx": _FakeUltraFaceSession(),
        "lr-asd-frontend.onnx": _FakeFrontendSession(),
        "lr-asd-backend.onnx": _FakeBackendSession(),
    }
    monkeypatch.setattr(sg, "_get_session", lambda name: fake_sessions[name])

    # Resize branch (180x200 != 240x320) + NMS deduplication
    rgb = np.zeros((180, 200, 3), dtype=np.uint8)
    faces = sg.detect_faces_ultraface(rgb, confidence=0.65)
    assert len(faces) == 1
    assert faces[0][4] == pytest.approx(0.92, rel=1e-3)

    # Direct _run_lr_asd_scores helper
    crops = np.full((30, 112, 112), 120.0, dtype=np.float32)
    mfcc_short = np.zeros((40, 13), dtype=np.float32)
    scores_direct = sg._run_lr_asd_scores(crops, mfcc_short)
    assert scores_direct.shape == (30,)

    # Early-exit guards
    dummy_vid = tmp_path / "slice.mp4"
    dummy_vid.write_bytes(b"fake")
    no_cam = sg.score_streamer_speech_asd(dummy_vid, None, [])
    assert no_cam["ran"] is False
    assert no_cam["reason"] == "no_webcam_or_words"

    def _iter_frames_stub(
        _video_path: Path,
        _webcam_box_px: tuple[int, int, int, int],
        *,
        fps: int = 25,
        pix_fmt: str = "rgb24",
    ) -> tuple[Iterator[np.ndarray], int, int]:
        if pix_fmt == "rgb24":
            return iter([np.zeros((120, 160, 3), dtype=np.uint8) for _ in range(12)]), 160, 120
        # 60 gray frames -> 2.4s at 25fps (triggers chunked frontend flush + tail flush)
        return iter([np.full((120, 160), 140, dtype=np.uint8) for _ in range(60)]), 160, 120

    monkeypatch.setattr(sg, "_iter_webcam_frames", _iter_frames_stub)
    monkeypatch.setattr(
        sg,
        "_extract_pcm_16k",
        lambda _p: (np.sin(np.linspace(0, 60.0, 16000 * 3)) * 5000).astype(np.int16),
    )

    med_face = sg.detect_face_center_ultraface(dummy_vid, (0, 0, 320, 240))
    assert med_face is not None
    assert len(med_face) == 4

    words = [
        WordTimestamp(word="Watch", start=0.20, end=0.55),
        WordTimestamp(word="this", start=0.60, end=0.90),
        WordTimestamp(word="Citizen", start=1.70, end=2.10),
        WordTimestamp(word="beyond", start=3.50, end=3.80),
    ]
    asd_res = sg.score_streamer_speech_asd(dummy_vid, (0, 0, 320, 240), words, logit_threshold=0.0)
    assert asd_res["ran"] is True
    assert asd_res["face_detected_ratio"] == 1.0
    assert words[0].is_npc_or_other is False
    assert words[2].is_npc_or_other is True
    assert asd_res["npc_words_marked"] >= 1
