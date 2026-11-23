"""Pillow freeze-frame callout generator aligned with adaptive <=1.8x layout and Montserrat Black (Hito 2.0)."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from src.schemas import EditPlan

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_BUNDLED_FONT = PROJECT_ROOT / "assets" / "fonts" / "Montserrat-Black.ttf"
_FALLBACK_FONT = Path(r"C:\Windows\Fonts\seguibl.ttf")


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for p in (_BUNDLED_FONT, _FALLBACK_FONT):
        if p.exists():
            try:
                return ImageFont.truetype(str(p), size)
            except OSError:
                continue
    return ImageFont.load_default()


def _compose_adaptive_9_16(src_img: Image.Image, layout: dict[str, Any] | None, webcam_box_px: tuple[int, int, int, int] | None) -> Image.Image:
    """Compose the exact 1080x1920 frame matching src/renderer.py adaptive layout."""
    sw, sh = src_img.size
    if layout and layout.get("layout_mode") == "full_gameplay_9_16":
        gx, gy, gw, gh = layout["gameplay_crop_px"]
        return src_img.crop((gx, gy, gx + gw, gy + gh)).resize((1080, 1920), Image.Resampling.LANCZOS).convert("RGBA")

    if layout and layout.get("gameplay_crop_px"):
        gx, gy, gw, gh = layout["gameplay_crop_px"]
        gp_crop = src_img.crop((gx, gy, gx + gw, gy + gh))
    else:
        gw = min(sw, round(sh * (1080.0 / 1228.0)))
        gx = (sw - gw) // 2
        gp_crop = src_img.crop((gx, 0, gx + gw, sh))

    bot_band = gp_crop.resize((1080, 1228), Image.Resampling.LANCZOS).convert("RGBA")

    cam_box = (layout.get("webcam_crop_px") if layout else None) or webcam_box_px
    if cam_box:
        cx, cy, cw, ch = cam_box
        cam_crop = src_img.crop((max(0, cx), max(0, cy), min(sw, cx + cw), min(sh, cy + ch)))
    else:
        cam_crop = gp_crop

    if layout and layout.get("is_framed_card") and layout.get("webcam_card_size"):
        card_w, card_h = layout["webcam_card_size"]
        bg = gp_crop.resize((1080, 692), Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(radius=18)).convert("RGBA")
        dark = Image.new("RGBA", (1080, 692), (0, 0, 0, 75))
        top_band = Image.alpha_composite(bg, dark)
        card = cam_crop.resize((card_w, card_h), Image.Resampling.LANCZOS).convert("RGBA")
        mask = Image.new("L", (card_w, card_h), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, card_w - 1, card_h - 1], radius=26, fill=255)
        px = (1080 - card_w) // 2
        py = max(0, min(692 - card_h - 10, max(172, 692 - card_h - 16)))
        top_band.paste(card, (px, py), mask)
        ImageDraw.Draw(top_band).rounded_rectangle(
            [px, py, px + card_w - 1, py + card_h - 1], radius=26, outline=(245, 248, 255, 165), width=4
        )
    else:
        tw, th = 1080, 692
        sc = max(tw / max(1, cam_crop.width), th / max(1, cam_crop.height))
        resized = cam_crop.resize((max(tw, round(cam_crop.width * sc)), max(th, round(cam_crop.height * sc))), Image.Resampling.LANCZOS)
        cl, ct = (resized.width - tw) // 2, (resized.height - th) // 2
        top_band = resized.crop((cl, ct, cl + tw, ct + th)).convert("RGBA")

    canvas = Image.new("RGBA", (1080, 1920), (0, 0, 0, 255))
    canvas.paste(top_band, (0, 0))
    canvas.paste(bot_band, (0, 692))
    return canvas


def render_freeze_callout(
    source_video: Path,
    edit_plan: EditPlan,
    out_png: Path,
    layout: dict[str, Any] | None = None,
) -> Path | None:
    """Render 1080x1920 freeze-frame PNG with ~45% vignette, spotlight box, and <=4 word Montserrat Black callout."""
    ff = edit_plan.freeze_frame
    if ff is None:
        return None

    t_src = max(0.0, edit_plan.start_trim + ff.timestamp_sec)
    with tempfile.TemporaryDirectory(prefix="cs_callout_") as tmp:
        raw_frame = Path(tmp) / "raw.png"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{t_src:.3f}",
                "-i",
                str(source_video),
                "-frames:v",
                "1",
                str(raw_frame),
            ],
            check=True,
        )
        src_img = Image.open(raw_frame).convert("RGBA")

    canvas = _compose_adaptive_9_16(src_img, layout, edit_plan.webcam_box_px)

    ymin, xmin, ymax, xmax = ff.box_2d
    bx0 = max(68, min(720, round(xmin * 1.08)))
    bx1 = max(bx0 + 140, min(872, round(xmax * 1.08)))
    by0 = max(708, min(1160, 692 + round(ymin * 1.228)))
    by1 = max(by0 + 120, min(1240, 692 + round(ymax * 1.228)))

    vignette_mask = Image.new("L", (1080, 1920), 115)
    ImageDraw.Draw(vignette_mask).rectangle([bx0, by0, bx1, by1], fill=0)
    dark_layer = Image.composite(
        Image.new("RGBA", (1080, 1920), (0, 0, 0, 130)),
        Image.new("RGBA", (1080, 1920), (0, 0, 0, 0)),
        vignette_mask,
    )
    composed = Image.alpha_composite(canvas, dark_layer)
    draw = ImageDraw.Draw(composed)
    draw.rectangle([bx0, by0, bx1, by1], outline=(0, 229, 255, 245), width=5)

    label = ff.callout_text.upper()
    font = _load_font(54)
    bbox = draw.textbbox((0, 0), label, font=font, stroke_width=5)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if tw > 760:
        font = _load_font(42)
        bbox = draw.textbbox((0, 0), label, font=font, stroke_width=5)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]

    tx = max(64, min(872 - tw, 540 - tw // 2))
    ty = max(600, min(780 - th, 692 - th // 2))
    draw.rounded_rectangle(
        [tx - 18, ty - 10, tx + tw + 18, ty + th + 14],
        radius=12,
        fill=(10, 12, 16, 215),
        outline=(255, 255, 0, 235),
        width=3,
    )
    draw.text((tx, ty), label, font=font, fill=(255, 255, 0, 255), stroke_width=5, stroke_fill=(0, 0, 0, 255))

    out_png.parent.mkdir(parents=True, exist_ok=True)
    composed.convert("RGB").save(out_png, format="PNG")
    return out_png
