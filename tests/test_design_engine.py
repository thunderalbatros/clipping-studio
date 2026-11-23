"""Unit & visual-contract tests for src/design_engine.py (APCA, KS-4 BiRefNet-lite, FFT Saliency, exact dims) and auto-editor."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from src.design_engine import (
    SAFE_BOX_16_9,
    UNIVERSAL_SAFE_BOX_9_16,
    compute_apca_lc,
    compute_spectral_saliency,
    extract_subject_cutout,
    render_editorial_cover_9_16,
    render_editorial_thumbnail_16_9,
)
from src.renderer import detect_segments_auto_editor
from src.schemas import AnnotatedTranscript


def test_apca_contrast_polarity_and_gate() -> None:
    # White text on near-black background exceeds |Lc| >= 100
    lc_white_on_dark = abs(compute_apca_lc((248, 250, 252), (8, 10, 16)))
    assert lc_white_on_dark >= 100.0
    # Low-contrast grey on grey fails 75 gate
    lc_low = abs(compute_apca_lc((130, 130, 130), (100, 100, 100)))
    assert lc_low < 40.0


def test_ks4_forbids_bria_rmbg_model() -> None:
    img = Image.new("RGB", (320, 180), (40, 50, 60))
    with pytest.raises(ValueError, match="KS-4"):
        extract_subject_cutout(img, model_name="bria-rmbg")


def test_guardrail4_mask_coverage_triggers_roi_fallback() -> None:
    """When BiRefNet-lite engulfs >72% of a complex 16:9 scene, extract_subject_cutout falls back cleanly."""
    img = Image.new("RGB", (640, 360), (40, 60, 45))

    def _opaque_blob(pil_img: Image.Image) -> Image.Image:
        rgba = pil_img.convert("RGBA")
        rgba.putalpha(255)  # 100% coverage -> violates max_coverage=0.72
        return rgba

    cutout, meta = extract_subject_cutout(img, model_name="birefnet-general-lite", rembg_fn=_opaque_blob)
    assert meta["used_roi_fallback"] is True
    assert meta["raw_full_coverage"] == 1.0
    assert cutout.mode == "RGBA"
    assert meta["mask_coverage"] < 0.90


def test_spectral_saliency_fft_detects_center_focal_object(tmp_path: Path) -> None:
    img = Image.new("RGB", (540, 960), (14, 16, 22))
    draw = ImageDraw.Draw(img)
    # High-contrast focal square strictly inside scaled safe box
    draw.rectangle([140, 340, 400, 520], fill=(255, 220, 40))
    heat_path = tmp_path / "sal_heat.jpg"
    res = compute_spectral_saliency(img, safe_box=(45, 210, 495, 625), out_heatmap_jpg=heat_path, apca_lc=92.5)
    assert heat_path.is_file()
    assert res["centroid_in_safe_box"] is True
    assert res["safe_box_energy_ratio"] >= 0.55


def test_editorial_cover_9_16_and_thumbnail_16_9_exact_dimensions_and_qa(tmp_path: Path) -> None:
    """Guardrail #2: verify exact (1080, 1920) & (1280, 720) via PIL.Image.size plus all 3 QA artifacts."""
    src_9_16 = tmp_path / "frame_9_16.png"
    img_v = Image.new("RGB", (1080, 1920), (24, 32, 48))
    ImageDraw.Draw(img_v).rectangle([200, 220, 880, 660], fill=(180, 130, 100))
    img_v.save(src_9_16)

    out_9_16 = tmp_path / "cover_9_16.jpg"
    rep_v = render_editorial_cover_9_16(src_9_16, "FIRE SPREADS FAST", out_9_16)
    assert Image.open(out_9_16).size == (1080, 1920)
    assert rep_v["apca_lc"] >= 75.0
    assert Path(rep_v["qa_artifacts"]["squint_160px"]).is_file()
    assert Image.open(rep_v["qa_artifacts"]["squint_160px"]).size[0] == 160
    assert Path(rep_v["qa_artifacts"]["crop_preview"]).is_file()
    assert Path(rep_v["qa_artifacts"]["saliency_heatmap"]).is_file()
    assert rep_v["safe_box"] == list(UNIVERSAL_SAFE_BOX_9_16)

    src_16_9 = tmp_path / "frame_16_9.png"
    img_h = Image.new("RGB", (1920, 1080), (35, 52, 30))
    ImageDraw.Draw(img_h).rectangle([1354, 640, 1920, 1080], fill=(195, 145, 115))
    img_h.save(src_16_9)

    out_16_9 = tmp_path / "thumbnail_1280x720.jpg"
    rep_h = render_editorial_thumbnail_16_9(src_16_9, "HE LOST IT", out_16_9, use_matting=False)
    assert Image.open(out_16_9).size == (1280, 720)
    assert rep_h["apca_lc"] >= 75.0
    assert Path(rep_h["qa_artifacts"]["squint_160px"]).is_file()
    assert Path(rep_h["qa_artifacts"]["crop_preview"]).is_file()
    assert Path(rep_h["qa_artifacts"]["saliency_heatmap"]).is_file()
    assert SAFE_BOX_16_9[2] <= 1280


def test_auto_editor_short_clip_graceful_bypass(tmp_path: Path) -> None:
    tr = AnnotatedTranscript(slice_id="s1", language="en", full_text="test")
    segs, info = detect_segments_auto_editor(
        source_slice=tmp_path / "missing.mp4",
        start_trim=0.0,
        clip_dur=10.0,
        transcript=tr,
    )
    assert segs is None
    assert info["used"] is False
    assert info["provider"] == "auto_editor"
