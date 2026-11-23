"""Pillow freeze-frame callout generator inside 2026 3-network red safe-zone (Hito 1.4)."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from src.schemas import EditPlan

_FONT_PATH = Path(r"C:\Windows\Fonts\seguibl.ttf")


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype(str(_FONT_PATH), size) if _FONT_PATH.exists() else ImageFont.load_default()
    except OSError:
        return ImageFont.load_default()


def _compose_9_16_frame(src_img: Image.Image, webcam_box_px: tuple[int, int, int, int] | None) -> Image.Image:
    """Compose the exact 1080x1920 split-screen (692px webcam + 1228px gameplay) used by renderer."""
    sw, sh = src_img.size
    if webcam_box_px:
        wx, wy, ww, wh = webcam_box_px
        wx = max(0, min(sw - 32, wx))
        wy = max(0, min(sh - 32, wy))
        ww = max(32, min(sw - wx, ww))
        wh = max(32, min(sh - wy, wh))
        cam_crop = src_img.crop((wx, wy, wx + ww, wy + wh))
    else:
        cw = max(64, int(sw * 0.42))
        ch = max(64, int(cw * 692 / 1080))
        cx = max(0, (sw - cw) // 2)
        cy = max(0, int(sh * 0.05))
        cam_crop = src_img.crop((cx, cy, min(sw, cx + cw), min(sh, cy + ch)))

    # Scale-to-fill 1080x692 preserving aspect ratio then center-crop
    tw, th = 1080, 692
    scale_cam = max(tw / cam_crop.width, th / cam_crop.height)
    cam_resized = cam_crop.resize(
        (max(tw, round(cam_crop.width * scale_cam)), max(th, round(cam_crop.height * scale_cam))),
        Image.Resampling.LANCZOS,
    )
    cl = (cam_resized.width - tw) // 2
    ct = (cam_resized.height - th) // 2
    top_band = cam_resized.crop((cl, ct, cl + tw, ct + th))

    # Gameplay band: scale height to 1228 preserving full vertical FOV, center-crop width to 1080
    gh = 1228
    gw_scaled = max(1080, round(sw * (gh / sh)))
    if gw_scaled % 2 == 1:
        gw_scaled += 1
    gp_resized = src_img.resize((gw_scaled, gh), Image.Resampling.LANCZOS)
    gl = (gp_resized.width - 1080) // 2
    bot_band = gp_resized.crop((gl, 0, gl + 1080, gh))

    canvas = Image.new("RGBA", (1080, 1920), (0, 0, 0, 255))
    canvas.paste(top_band.convert("RGBA"), (0, 0))
    canvas.paste(bot_band.convert("RGBA"), (0, 692))
    return canvas


def render_freeze_callout(source_video: Path, edit_plan: EditPlan, out_png: Path) -> Path | None:
    """Render a 1080x1920 freeze-frame PNG with ~45% vignette, spotlight box, and <=4 word Segoe UI Black callout."""
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

    canvas = _compose_9_16_frame(src_img, edit_plan.webcam_box_px)

    # Map normalized 0..1000 box_2d into gameplay band inside safe horizontal bounds [70..860]
    ymin, xmin, ymax, xmax = ff.box_2d
    bx0 = max(68, min(720, round(xmin * 1.08)))
    bx1 = max(bx0 + 140, min(872, round(xmax * 1.08)))
    by0 = max(708, min(1160, 692 + round(ymin * 1.228)))
    by1 = max(by0 + 120, min(1320, 692 + round(ymax * 1.228)))

    # ~45% vignette outside focal rectangle while keeping focal rectangle bright
    vignette_mask = Image.new("L", (1080, 1920), 115)  # 115/255 ~= 45% darken
    mdraw = ImageDraw.Draw(vignette_mask)
    mdraw.rectangle([bx0, by0, bx1, by1], fill=0)
    dark_layer = Image.composite(
        Image.new("RGBA", (1080, 1920), (0, 0, 0, 130)),
        Image.new("RGBA", (1080, 1920), (0, 0, 0, 0)),
        vignette_mask,
    )
    composed = Image.alpha_composite(canvas, dark_layer)

    draw = ImageDraw.Draw(composed)
    draw.rectangle([bx0, by0, bx1, by1], outline=(0, 229, 255, 245), width=5)

    # Render <=4-word callout inside Red Safe Box [48..888, 288..1248] right above the seam/callout box
    label = ff.callout_text.upper()
    font = _load_font(58)
    bbox = draw.textbbox((0, 0), label, font=font, stroke_width=4)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    if tw > 790:
        font = _load_font(46)
        bbox = draw.textbbox((0, 0), label, font=font, stroke_width=4)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]

    safe_center_x = (48 + 888) // 2  # 468, keeps text well away from right_pct 18%
    tx = max(60, min(870 - tw, safe_center_x - tw // 2))
    ty = max(590, min(790 - th, 692 - th // 2))

    # Clean kinetic accent pill behind label inside red safe box
    draw.rounded_rectangle([tx - 18, ty - 10, tx + tw + 18, ty + th + 14], radius=12, fill=(10, 12, 16, 205), outline=(0, 229, 255, 220), width=3)
    draw.text((tx, ty), label, font=font, fill=(255, 230, 0, 255), stroke_width=4, stroke_fill=(0, 0, 0, 255))

    out_png.parent.mkdir(parents=True, exist_ok=True)
    composed.convert("RGB").save(out_png, format="PNG")
    return out_png
