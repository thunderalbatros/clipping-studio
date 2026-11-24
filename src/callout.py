"""Senior Editorial Motion-Graphics Alpha Overlay Engine (v3.5 — HTML5/CSS3 Chrome CDP + Pillow RGBA fallback).

Key design guarantees:
- Default mode (`mode="motion_overlay"`): renders a short (1.5-2.5s) 1080x460 transparent RGBA video
  (`callout_alpha.mov`) that composites over LIVE moving video via `FFmpeg overlay` — ZERO dead freeze-frame!
- Uses Chrome/Edge Headless over local CDP WebSocket (`Emulation.setDefaultBackgroundColorOverride` alpha=0)
  with embedded `Montserrat-Black.ttf`, 2-color hero editorial typography (white + gold/red accent),
  soft radial dark scrim (NEVER a tiny yellow-bordered box), and real cubic-bezier overshoot pop-in + slide/fade-out.
- Automatic deterministic Pillow RGBA fallback when Chrome is absent (e.g. Linux CI runners).
- Explicit opt-in `mode="comedic_freeze"` only for deliberate comedic punchlines (1.14x quick zoom + vignette + hit SFX).
"""

from __future__ import annotations

import asyncio
import base64
import html as html_lib
import json
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from src.runtime_paths import (
    BUNDLED_FONT_PATH as _BUNDLED_FONT,
)
from src.runtime_paths import (
    find_headless_browser,
    headless_browser_flags,
    load_bundled_font,
)
from src.schemas import UNIVERSAL_SAFE_BOX_9_16, EditPlan, FreezeFrameSpec

OVERLAY_BAND_W = 1080
OVERLAY_BAND_H = 440
# Positioned right below the webcam seam (y=678) inside the shared UNIVERSAL_SAFE_BOX_9_16 [48..888, 420..1248]
# and cleanly above bottom captions (y=1120) so overlay and captions never collide.
OVERLAY_DEFAULT_Y = 678


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return load_bundled_font(size, font_path=_BUNDLED_FONT)


def _font_base64() -> str:
    if _BUNDLED_FONT.is_file():
        return base64.b64encode(_BUNDLED_FONT.read_bytes()).decode("ascii")
    return ""


def _find_browser_binary() -> Path | None:
    return find_headless_browser()


def _cubic_bezier_eval(t: float, p1x: float, p1y: float, p2x: float, p2y: float) -> float:
    """Evaluate 1D CSS cubic-bezier(p1x, p1y, p2x, p2y) easing at normalized progress t in [0, 1]."""
    t = max(0.0, min(1.0, float(t)))
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    # Newton-Raphson solve for curve parameter u where Bx(u) == t
    u = t
    for _ in range(6):
        inv = 1.0 - u
        bx = 3.0 * inv * inv * u * p1x + 3.0 * inv * u * u * p2x + u * u * u
        dbx = 3.0 * inv * inv * p1x + 6.0 * inv * u * (p2x - p1x) + 3.0 * u * u * (1.0 - p2x)
        if abs(dbx) < 1e-6:
            break
        u = max(0.0, min(1.0, u - (bx - t) / dbx))
    inv = 1.0 - u
    return 3.0 * inv * inv * u * p1y + 3.0 * inv * u * u * p2y + u * u * u


def _split_editorial_lines(text: str) -> list[str]:
    words = [w.upper() for w in text.strip().split() if w][:4]
    if not words:
        return ["WAIT FOR", "IT"]
    if len(words) <= 2:
        return [" ".join(words)]
    mid = (len(words) + 1) // 2
    return [" ".join(words[:mid]), " ".join(words[mid:])]


def _build_motion_overlay_html(
    callout_text: str,
    accent_color: str = "#FFD400",
    eyebrow_text: str = "",
    width: int = OVERLAY_BAND_W,
    height: int = OVERLAY_BAND_H,
) -> str:
    """Build deterministic HTML5/CSS3 editorial motion-graphics canvas driven by `window.__renderAt(t, dur)`."""
    lines = _split_editorial_lines(callout_text)
    font_b64 = _font_base64()
    max_line_chars = max(len(ln) for ln in lines)
    font_size = 94 if max_line_chars <= 10 else (84 if max_line_chars <= 13 else 74)
    total_words = sum(len(ln.split()) for ln in lines)
    accent_safe = accent_color if accent_color.startswith("#") and len(accent_color) in {4, 7} else "#FFD400"

    word_idx = 0
    lines_html: list[str] = []
    for li, ln in enumerate(lines):
        spans: list[str] = []
        for w in ln.split():
            word_idx += 1
            esc = html_lib.escape(w)
            if word_idx == total_words and total_words >= 2:
                spans.append(f'<span class="accent">{esc}</span>')
            else:
                spans.append(f'<span class="base">{esc}</span>')
        lines_html.append(f'<div class="hl-line" id="line-{li}">{" ".join(spans)}</div>')

    eyebrow_html = (
        f'<div class="eyebrow" id="eyebrow">{html_lib.escape(eyebrow_text.strip().upper()[:18])}</div>'
        if eyebrow_text.strip()
        else ""
    )

    font_css = (
        f"@font-face {{ font-family: 'MontserratBlack'; src: url('data:font/ttf;base64,{font_b64}') format('truetype'); "
        "font-weight: 900; font-style: normal; font-display: block; }"
        if font_b64
        else ""
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<style>
  {font_css}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html, body {{
    width: {width}px;
    height: {height}px;
    overflow: hidden;
    background: transparent !important;
    font-family: 'MontserratBlack', 'Montserrat', 'Arial Black', sans-serif;
    -webkit-font-smoothing: antialiased;
  }}
  .stage {{
    position: fixed;
    left: 0;
    top: 0;
    width: {width}px;
    height: {height}px;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 0 {max(0, width - UNIVERSAL_SAFE_BOX_9_16[2])}px 0 {UNIVERSAL_SAFE_BOX_9_16[0]}px;
  }}
  .scrim {{
    position: absolute;
    inset: 14px {max(0, width - UNIVERSAL_SAFE_BOX_9_16[2])}px 14px {UNIVERSAL_SAFE_BOX_9_16[0]}px;
    border-radius: 999px;
    background: radial-gradient(
      ellipse at center,
      rgba(5, 7, 13, 0.74) 0%,
      rgba(5, 7, 13, 0.44) 48%,
      rgba(5, 7, 13, 0.0) 80%
    );
    opacity: 0;
    transform: scale(0.88);
    pointer-events: none;
  }}
  .eyebrow {{
    position: relative;
    z-index: 2;
    margin-bottom: 12px;
    padding: 6px 20px;
    border-radius: 999px;
    background: {accent_safe};
    color: #080A10;
    font-size: 24px;
    font-weight: 900;
    letter-spacing: 0.14em;
    box-shadow: 0 8px 20px rgba(0, 0, 0, 0.65);
    opacity: 0;
    transform: translateY(16px) scale(0.78);
  }}
  .text-stack {{
    position: relative;
    z-index: 2;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    text-align: center;
    gap: 2px;
  }}
  .hl-line {{
    font-size: {font_size}px;
    font-weight: 900;
    line-height: 0.96;
    letter-spacing: -0.02em;
    text-transform: uppercase;
    white-space: nowrap;
    color: #FFFFFF;
    -webkit-text-stroke: 3.5px rgba(6, 8, 14, 0.95);
    paint-order: stroke fill;
    filter: drop-shadow(0 8px 0 rgba(4, 6, 12, 0.94)) drop-shadow(0 16px 26px rgba(0, 0, 0, 0.88));
    opacity: 0;
    transform: translateY(36px) scale(0.64);
    transform-origin: center center;
  }}
  .accent {{
    color: {accent_safe};
    filter: drop-shadow(0 0 22px rgba(255, 212, 0, 0.45));
  }}
  .accent-bar {{
    position: relative;
    z-index: 2;
    margin-top: 16px;
    width: 240px;
    height: 7px;
    border-radius: 999px;
    background: linear-gradient(90deg, rgba(255,212,0,0) 0%, {accent_safe} 28%, #FFFFFF 50%, {accent_safe} 72%, rgba(255,212,0,0) 100%);
    box-shadow: 0 4px 18px rgba(255, 212, 0, 0.52);
    opacity: 0;
    transform: scaleX(0);
    transform-origin: center center;
  }}
</style>
</head>
<body>
  <div class="stage" id="stage">
    <div class="scrim" id="scrim"></div>
    {eyebrow_html}
    <div class="text-stack" id="stack">
      {"".join(lines_html)}
    </div>
    <div class="accent-bar" id="bar"></div>
  </div>
<script>
  function clamp01(v) {{ return Math.max(0, Math.min(1, v)); }}
  function cubicBezier(t, p1x, p1y, p2x, p2y) {{
    t = clamp01(t);
    if (t <= 0) return 0;
    if (t >= 1) return 1;
    let u = t;
    for (let i = 0; i < 6; i++) {{
      const inv = 1 - u;
      const bx = 3 * inv * inv * u * p1x + 3 * inv * u * u * p2x + u * u * u;
      const dbx = 3 * inv * inv * p1x + 6 * inv * u * (p2x - p1x) + 3 * u * u * (1 - p2x);
      if (Math.abs(dbx) < 1e-6) break;
      u = clamp01(u - (bx - t) / dbx);
    }}
    const inv = 1 - u;
    return 3 * inv * inv * u * p1y + 3 * inv * u * u * p2y + u * u * u;
  }}
  window.__renderAt = function(t, dur) {{
    const inDur = Math.min(0.30, dur * 0.22);
    const outDur = Math.min(0.28, dur * 0.20);
    const outStart = Math.max(inDur + 0.1, dur - outDur);
    const pOut = t > outStart ? clamp01((t - outStart) / Math.max(0.05, outDur)) : 0;
    const easeOutExit = cubicBezier(pOut, 0.42, 0.0, 1.0, 1.0);
    const holdFrac = clamp01((t - inDur) / Math.max(0.1, outStart - inDur));

    // Soft radial scrim fades smoothly in & out without blocking moving video
    const scrim = document.getElementById('scrim');
    const pScrimIn = clamp01(t / Math.max(0.05, inDur * 0.9));
    const scrimOp = cubicBezier(pScrimIn, 0.16, 1, 0.3, 1) * (1 - easeOutExit);
    scrim.style.opacity = scrimOp.toFixed(4);
    scrim.style.transform = `scale(${{(0.88 + 0.14 * scrimOp).toFixed(4)}})`;

    const eyebrow = document.getElementById('eyebrow');
    if (eyebrow) {{
      const pEye = clamp01(t / Math.max(0.05, inDur * 0.85));
      const eEye = cubicBezier(pEye, 0.34, 1.56, 0.64, 1.0);
      const opEye = clamp01(pEye * 2.2) * (1 - easeOutExit);
      const yEye = (1 - eEye) * 18 - easeOutExit * 22;
      const sEye = (0.76 + 0.24 * eEye) * (1 - 0.06 * easeOutExit);
      eyebrow.style.opacity = opEye.toFixed(4);
      eyebrow.style.transform = `translateY(${{yEye.toFixed(2)}}px) scale(${{sEye.toFixed(4)}})`;
    }}

    const lines = document.querySelectorAll('.hl-line');
    lines.forEach((el, idx) => {{
      const delay = idx * 0.065;
      const pIn = clamp01((t - delay) / Math.max(0.06, inDur));
      const pop = cubicBezier(pIn, 0.34, 1.56, 0.64, 1.0);
      const opIn = clamp01(pIn * 2.4);
      const op = opIn * (1 - easeOutExit);
      const driftScale = 1.0 + 0.032 * holdFrac;
      const scale = (0.64 + 0.36 * pop) * driftScale * (1 - 0.08 * easeOutExit);
      const y = (1 - pop) * 34 - easeOutExit * 26;
      el.style.opacity = op.toFixed(4);
      el.style.transform = `translateY(${{y.toFixed(2)}}px) scale(${{scale.toFixed(4)}})`;
    }});

    const bar = document.getElementById('bar');
    const pBar = clamp01((t - 0.08) / Math.max(0.06, inDur));
    const eBar = cubicBezier(pBar, 0.22, 1.0, 0.36, 1.0);
    const barOp = eBar * (1 - easeOutExit);
    const barSx = Math.max(0.001, eBar * (1 - easeOutExit));
    bar.style.opacity = barOp.toFixed(4);
    bar.style.transform = `scaleX(${{barSx.toFixed(4)}})`;
    return true;
  }};
  window.__renderAt(0.5, 1.8);
</script>
</body>
</html>"""


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _capture_frames_cdp_async(
    ws_url: str,
    html_uri: str,
    frames_dir: Path,
    duration_sec: float,
    fps: int,
    width: int,
    height: int,
) -> int:
    import websockets

    msg_id = 0

    async def _send(ws: Any, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        nonlocal msg_id
        msg_id += 1
        req_id = msg_id
        payload: dict[str, Any] = {"id": req_id, "method": method}
        if params:
            payload["params"] = params
        await ws.send(json.dumps(payload))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=15.0)
            data = json.loads(raw)
            if data.get("id") == req_id:
                if "error" in data:
                    raise RuntimeError(f"CDP {method} failed: {data['error']}")
                return dict(data.get("result") or {})

    total_frames = max(6, round(duration_sec * fps))
    async with websockets.connect(ws_url, max_size=32 * 1024 * 1024) as ws:
        await _send(ws, "Page.enable")
        await _send(ws, "Runtime.enable")
        await _send(
            ws,
            "Emulation.setDeviceMetricsOverride",
            {
                "width": width,
                "height": height,
                "deviceScaleFactor": 1,
                "mobile": False,
                "screenWidth": width,
                "screenHeight": height,
            },
        )
        await _send(
            ws,
            "Emulation.setDefaultBackgroundColorOverride",
            {"color": {"r": 0, "g": 0, "b": 0, "a": 0}},
        )
        await _send(ws, "Page.navigate", {"url": html_uri})
        # Wait for font & DOM readiness
        for _ in range(30):
            res = await _send(
                ws,
                "Runtime.evaluate",
                {
                    "expression": "(async () => { await document.fonts.ready; return typeof window.__renderAt === 'function'; })()",
                    "awaitPromise": True,
                    "returnByValue": True,
                },
            )
            if (res.get("result") or {}).get("value") is True:
                break
            await asyncio.sleep(0.05)

        # Re-apply exact viewport metrics AFTER file:// navigation so Chrome never retains OS DPI / default viewport offset
        await _send(
            ws,
            "Emulation.setDeviceMetricsOverride",
            {
                "width": width,
                "height": height,
                "deviceScaleFactor": 1,
                "mobile": False,
                "screenWidth": width,
                "screenHeight": height,
            },
        )
        await _send(
            ws,
            "Emulation.setDefaultBackgroundColorOverride",
            {"color": {"r": 0, "g": 0, "b": 0, "a": 0}},
        )

        for idx in range(total_frames):
            t = idx / float(fps)
            await _send(
                ws,
                "Runtime.evaluate",
                {
                    "expression": f"window.__renderAt({t:.4f}, {duration_sec:.4f})",
                    "returnByValue": True,
                },
            )
            shot = await _send(
                ws,
                "Page.captureScreenshot",
                {
                    "format": "png",
                    "fromSurface": True,
                    "clip": {"x": 0, "y": 0, "width": width, "height": height, "scale": 1},
                },
            )
            png_bytes = base64.b64decode(shot["data"])
            out_p = frames_dir / f"frame_{idx:04d}.png"
            out_p.write_bytes(png_bytes)
            with Image.open(out_p) as im:
                if im.size != (width, height):
                    im.convert("RGBA").resize((width, height), Image.Resampling.LANCZOS).save(out_p, format="PNG")

    return total_frames


def _render_frames_via_chrome_cdp(
    html_doc: str,
    frames_dir: Path,
    duration_sec: float,
    fps: int = 30,
    width: int = OVERLAY_BAND_W,
    height: int = OVERLAY_BAND_H,
) -> tuple[bool, str]:
    """Render transparent RGBA PNG sequence in a single warm Chrome Headless CDP session."""
    browser = _find_browser_binary()
    if browser is None:
        return False, "no_browser"

    port = _pick_free_port()
    user_data_dir = frames_dir / "_chrome_prof"
    user_data_dir.mkdir(parents=True, exist_ok=True)
    html_path = frames_dir / "overlay.html"
    html_path.write_text(html_doc, encoding="utf-8")

    proc = subprocess.Popen(
        [
            str(browser),
            *headless_browser_flags(),
            "--disable-background-networking",
            "--mute-audio",
            f"--window-size={width},{height}",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data_dir}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        ws_url = ""
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return False, "browser_exited_early"
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1.0) as resp:
                    targets = json.loads(resp.read().decode("utf-8"))
                page_target = next((t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")), None)
                if page_target:
                    ws_url = str(page_target["webSocketDebuggerUrl"])
                    break
            except Exception:
                time.sleep(0.12)
        if not ws_url:
            return False, "cdp_timeout"

        asyncio.run(
            _capture_frames_cdp_async(
                ws_url=ws_url,
                html_uri=html_path.as_uri(),
                frames_dir=frames_dir,
                duration_sec=duration_sec,
                fps=fps,
                width=width,
                height=height,
            )
        )
        return True, f"chrome_cdp:{browser.name}"
    except Exception as exc:
        return False, f"cdp_error:{exc}"
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=4)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        shutil.rmtree(user_data_dir, ignore_errors=True)


def _hex_to_rgb(hex_color: str, default: tuple[int, int, int] = (255, 212, 0)) -> tuple[int, int, int]:
    c = hex_color.strip().lstrip("#")
    if len(c) == 6:
        try:
            return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))
        except ValueError:
            return default
    return default


def _render_frames_pillow_fallback(
    callout_text: str,
    frames_dir: Path,
    duration_sec: float,
    fps: int = 30,
    accent_color: str = "#FFD400",
    width: int = OVERLAY_BAND_W,
    height: int = OVERLAY_BAND_H,
) -> int:
    """Deterministic Pillow RGBA fallback with hero Montserrat Black, radial scrim, and cubic-bezier animation."""
    lines = _split_editorial_lines(callout_text)
    accent_rgb = _hex_to_rgb(accent_color)
    max_chars = max(len(ln) for ln in lines)
    font_sz = 92 if max_chars <= 10 else (82 if max_chars <= 13 else 72)
    font = _load_font(font_sz)

    # Pre-render static high-res editorial text layer (transparent RGBA) strictly inside UNIVERSAL_SAFE_BOX_9_16
    safe_x0 = UNIVERSAL_SAFE_BOX_9_16[0]
    safe_x1 = min(width, UNIVERSAL_SAFE_BOX_9_16[2])
    safe_w = max(200, safe_x1 - safe_x0)
    text_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(text_layer)
    total_words = sum(len(ln.split()) for ln in lines)

    def _measure_lines(fnt: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> tuple[list[tuple[list[tuple[str, bool, int]], int, int]], int]:
        metrics: list[tuple[list[tuple[str, bool, int]], int, int]] = []
        w_idx = 0
        max_lw = 0
        for ln in lines:
            toks: list[tuple[str, bool, int]] = []
            lw = 0
            lh = 0
            parts = ln.split()
            for pi, w in enumerate(parts):
                w_idx += 1
                is_acc = w_idx == total_words and total_words >= 2
                seg = w + (" " if pi + 1 < len(parts) else "")
                bb = draw.textbbox((0, 0), seg, font=fnt, stroke_width=6)
                tw = round(bb[2] - bb[0])
                th = round(bb[3] - bb[1])
                toks.append((seg, is_acc, tw))
                lw += tw
                lh = max(lh, th)
            max_lw = max(max_lw, lw)
            metrics.append((toks, lw, lh))
        return metrics, max_lw

    line_metrics, widest_lw = _measure_lines(font)
    while widest_lw > safe_w and font_sz > 38:
        font_sz -= 6
        font = _load_font(font_sz)
        line_metrics, widest_lw = _measure_lines(font)

    total_h = sum(m[2] for m in line_metrics) + max(0, len(line_metrics) - 1) * 14
    cy = (height - total_h) // 2 - 14
    for toks, lw, lh in line_metrics:
        cx = safe_x0 + max(0, (safe_w - lw) // 2)
        for seg, is_acc, tw in toks:
            fill = (*accent_rgb, 255) if is_acc else (255, 255, 255, 255)
            # Hard editorial drop shadow
            draw.text((cx, cy + 8), seg, font=font, fill=(4, 6, 12, 235), stroke_width=6, stroke_fill=(4, 6, 12, 235))
            draw.text((cx, cy), seg, font=font, fill=fill, stroke_width=5, stroke_fill=(6, 8, 14, 255))
            cx += tw
        cy += lh + 14

    # Soft radial dark scrim (elliptical Gaussian falloff, zero rectangular border)
    yy, xx = np.mgrid[0:height, 0:width]
    rx = width * 0.40
    ry = height * 0.36
    dist = np.sqrt(((xx - width / 2.0) / rx) ** 2 + ((yy - height / 2.0) / ry) ** 2)
    scrim_a = np.clip((1.0 - dist / 1.15), 0.0, 1.0) ** 1.7 * 185.0
    scrim_rgba = np.zeros((height, width, 4), dtype=np.uint8)
    scrim_rgba[..., 0] = 5
    scrim_rgba[..., 1] = 7
    scrim_rgba[..., 2] = 13
    scrim_rgba[..., 3] = scrim_a.astype(np.uint8)
    scrim_base = Image.fromarray(scrim_rgba, mode="RGBA")

    total_frames = max(6, round(duration_sec * fps))
    in_dur = min(0.30, duration_sec * 0.22)
    out_dur = min(0.28, duration_sec * 0.20)
    out_start = max(in_dur + 0.1, duration_sec - out_dur)

    for idx in range(total_frames):
        t = idx / float(fps)
        p_in = min(1.0, max(0.0, t / max(0.06, in_dur)))
        pop = _cubic_bezier_eval(p_in, 0.34, 1.56, 0.64, 1.0)
        p_out = min(1.0, max(0.0, (t - out_start) / max(0.05, out_dur))) if t > out_start else 0.0
        exit_e = _cubic_bezier_eval(p_out, 0.42, 0.0, 1.0, 1.0)
        hold_f = min(1.0, max(0.0, (t - in_dur) / max(0.1, out_start - in_dur)))

        opacity = min(1.0, p_in * 2.4) * (1.0 - exit_e)
        scale = max(0.25, (0.64 + 0.36 * pop) * (1.0 + 0.03 * hold_f) * (1.0 - 0.08 * exit_e))
        dy = round((1.0 - pop) * 34.0 - exit_e * 26.0)

        frame = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        if opacity > 0.01:
            sc_copy = scrim_base.copy()
            sc_a = sc_copy.getchannel("A").point(lambda v, op=opacity: round(v * op))
            sc_copy.putalpha(sc_a)
            frame = Image.alpha_composite(frame, sc_copy)

            sw = max(2, round(width * scale))
            sh = max(2, round(height * scale))
            scaled = text_layer.resize((sw, sh), Image.Resampling.LANCZOS)
            ta = scaled.getchannel("A").point(lambda v, op=opacity: round(v * op))
            scaled.putalpha(ta)
            px = (width - sw) // 2
            py = (height - sh) // 2 + dy
            temp_t = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            temp_t.paste(scaled, (px, py), scaled)
            frame = Image.alpha_composite(frame, temp_t)

            # Accent underline bar
            bar_w = round(240 * max(0.0, min(1.0, pop)) * (1.0 - exit_e))
            if bar_w >= 8:
                bdraw = ImageDraw.Draw(frame)
                bx0 = (width - bar_w) // 2
                by0 = min(height - 32, cy + 8 + dy)
                bdraw.rounded_rectangle(
                    [bx0, by0, bx0 + bar_w, by0 + 7],
                    radius=4,
                    fill=(*accent_rgb, round(240 * opacity)),
                )

        frame.save(frames_dir / f"frame_{idx:04d}.png", format="PNG")
    return total_frames


def render_motion_callout_alpha(
    spec: FreezeFrameSpec,
    out_mov: Path,
    fps: int = 30,
    prefer_chrome: bool = True,
) -> dict[str, Any]:
    """Render an animated transparent RGBA motion-graphics overlay video (`callout_alpha.mov`, ZERO freeze-frame)."""
    dur = max(1.2, min(3.0, float(spec.duration_sec if spec.duration_sec >= 1.2 else 1.8)))
    out_mov.parent.mkdir(parents=True, exist_ok=True)
    html_doc = _build_motion_overlay_html(
        callout_text=spec.callout_text,
        accent_color=spec.accent_color,
        eyebrow_text=spec.eyebrow_text,
        width=OVERLAY_BAND_W,
        height=OVERLAY_BAND_H,
    )

    with tempfile.TemporaryDirectory(prefix="cs_mograph_") as tmp:
        frames_dir = Path(tmp)
        engine = "pillow_rgba_fallback"
        ok_chrome = False
        if prefer_chrome:
            ok_chrome, engine_detail = _render_frames_via_chrome_cdp(
                html_doc=html_doc,
                frames_dir=frames_dir,
                duration_sec=dur,
                fps=fps,
                width=OVERLAY_BAND_W,
                height=OVERLAY_BAND_H,
            )
            if ok_chrome:
                engine = engine_detail
        if not ok_chrome:
            _render_frames_pillow_fallback(
                callout_text=spec.callout_text,
                frames_dir=frames_dir,
                duration_sec=dur,
                fps=fps,
                accent_color=spec.accent_color,
                width=OVERLAY_BAND_W,
                height=OVERLAY_BAND_H,
            )

        # Verify at least frame 0 exists and carries RGBA alpha channel
        mid_frame = frames_dir / f"frame_{max(0, round(dur * fps * 0.4)):04d}.png"
        has_alpha = False
        if mid_frame.is_file():
            with Image.open(mid_frame) as im:
                has_alpha = "A" in im.getbands()

        res = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-framerate",
                str(fps),
                "-i",
                str(frames_dir / "frame_%04d.png"),
                "-c:v",
                "png",
                "-pix_fmt",
                "rgba",
                str(out_mov.resolve()),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=45,
        )
        if res.returncode != 0 or not out_mov.is_file():
            raise RuntimeError(f"Alpha overlay encode failed: {(res.stderr or '')[-240:]}")

    return {
        "path": out_mov,
        "mode": "motion_overlay",
        "engine": engine,
        "width": OVERLAY_BAND_W,
        "height": OVERLAY_BAND_H,
        "overlay_y": OVERLAY_DEFAULT_Y,
        "duration_sec": round(dur, 3),
        "fps": fps,
        "has_alpha": has_alpha,
        "sfx": spec.sfx,
        "sfx_lead_frames": 2,
    }


def _build_ambient_webcam_backdrop(cam_crop: Image.Image, target_size: tuple[int, int] = (1080, 692)) -> Image.Image:
    """Create a subtle darkened/blurred ambient studio backdrop derived ONLY from the webcam (zero gameplay bleed)."""
    tw, th = target_size
    sc = max(tw / max(1, cam_crop.width), th / max(1, cam_crop.height))
    bg = cam_crop.resize((max(tw, round(cam_crop.width * sc)), max(th, round(cam_crop.height * sc))), Image.Resampling.BILINEAR)
    cl, ct = (bg.width - tw) // 2, (bg.height - th) // 2
    bg = bg.crop((cl, ct, cl + tw, ct + th)).convert("RGB")
    bg = bg.filter(ImageFilter.GaussianBlur(radius=30))
    bg = ImageEnhance.Color(bg).enhance(0.75)
    bg = ImageEnhance.Brightness(bg).enhance(0.52)
    # Smooth dark radial vignette so edges settle cleanly into studio tone without black dead bars
    yy, xx = np.mgrid[0:th, 0:tw]
    dist = np.sqrt(((xx - tw * 0.5) / (tw * 0.62)) ** 2 + ((yy - th * 0.55) / (th * 0.68)) ** 2)
    vig = np.clip(1.0 - 0.38 * (dist**1.6), 0.52, 1.0)[..., None]
    arr = np.clip(np.asarray(bg, dtype=np.float32) * vig, 0, 255).astype(np.uint8)
    return Image.fromarray(arr, mode="RGB").convert("RGBA")


def _compose_adaptive_9_16(
    src_img: Image.Image,
    layout: dict[str, Any] | None,
    webcam_box_px: tuple[int, int, int, int] | None,
) -> Image.Image:
    """Compose the exact 1080x1920 frame matching src/renderer.py adaptive layout (v3.5 ambient webcam studio backdrop)."""
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
        top_band = _build_ambient_webcam_backdrop(cam_crop, (1080, 692))
        card = cam_crop.resize((card_w, card_h), Image.Resampling.LANCZOS).convert("RGBA")
        mask = Image.new("L", (card_w, card_h), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, card_w - 1, card_h - 1], radius=24, fill=255)
        px = (1080 - card_w) // 2
        py = max(0, min(692 - card_h - 10, max(180, 692 - card_h - 16)))
        top_band.paste(card, (px, py), mask)
        ImageDraw.Draw(top_band).rounded_rectangle(
            [px, py, px + card_w - 1, py + card_h - 1], radius=24, outline=(245, 248, 255, 170), width=4
        )
    else:
        tw, th = 1080, 692
        sc = max(tw / max(1, cam_crop.width), th / max(1, cam_crop.height))
        resized = cam_crop.resize(
            (max(tw, round(cam_crop.width * sc)), max(th, round(cam_crop.height * sc))),
            Image.Resampling.LANCZOS,
        )
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
    """Explicit opt-in comedic freeze-frame PNG (`mode='comedic_freeze'` only: 1.14x punch-zoom + vignette + hero text)."""
    ff = edit_plan.freeze_frame
    if ff is None or ff.mode != "comedic_freeze":
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
        with Image.open(raw_frame) as _im:
            src_img = _im.convert("RGBA")

    canvas = _compose_adaptive_9_16(src_img, layout, edit_plan.webcam_box_px)
    # 1.14x punch-in comedic zoom on the full composed frame
    zw, zh = round(1080 * 1.14), round(1920 * 1.14)
    zoomed = canvas.resize((zw, zh), Image.Resampling.LANCZOS)
    zl, zt = (zw - 1080) // 2, (zh - 1920) // 2
    canvas = zoomed.crop((zl, zt, zl + 1080, zt + 1920))

    # Dramatic radial vignette
    yy, xx = np.mgrid[0:1920, 0:1080]
    dist = np.sqrt(((xx - 540.0) / 540.0) ** 2 + ((yy - 960.0) / 960.0) ** 2)
    vig_a = np.clip((dist - 0.35) * 190.0, 0.0, 175.0).astype(np.uint8)
    vig_arr = np.zeros((1920, 1080, 4), dtype=np.uint8)
    vig_arr[..., 3] = vig_a
    composed = Image.alpha_composite(canvas, Image.fromarray(vig_arr, mode="RGBA"))

    # Render hero editorial text at seam using the Pillow RGBA engine at peak visibility
    with tempfile.TemporaryDirectory(prefix="cs_cf_txt_") as tmp2:
        tdir = Path(tmp2)
        _render_frames_pillow_fallback(ff.callout_text, tdir, duration_sec=0.4, fps=10, accent_color=ff.accent_color)
        peak_p = tdir / "frame_0002.png"
        if peak_p.is_file():
            with Image.open(peak_p) as _im:
                ov_band = _im.convert("RGBA")
            composed.alpha_composite(ov_band, dest=(0, OVERLAY_DEFAULT_Y))

    out_png.parent.mkdir(parents=True, exist_ok=True)
    composed.convert("RGB").save(out_png, format="PNG")
    return out_png


def render_editorial_callout(
    source_video: Path,
    edit_plan: EditPlan,
    work_dir: Path,
    layout: dict[str, Any] | None = None,
    prefer_chrome: bool = True,
) -> dict[str, Any] | None:
    """Unified v3.5 callout dispatcher: animated RGBA motion_overlay by default; comedic_freeze only if explicitly enabled."""
    ff = edit_plan.freeze_frame
    if ff is None:
        return None
    if ff.mode == "comedic_freeze":
        png = render_freeze_callout(source_video, edit_plan, work_dir / "callout_freeze.png", layout=layout)
        if png is None or not png.is_file():
            return None
        return {
            "path": png,
            "mode": "comedic_freeze",
            "engine": "pillow_comedic_freeze",
            "width": 1080,
            "height": 1920,
            "overlay_y": 0,
            "duration_sec": min(1.0, max(0.5, float(ff.duration_sec))),
            "fps": 30,
            "has_alpha": False,
            "sfx": "hit" if ff.sfx == "whoosh" else ff.sfx,
            "sfx_lead_frames": 2,
        }
    return render_motion_callout_alpha(
        spec=ff,
        out_mov=work_dir / "callout_alpha.mov",
        fps=30,
        prefer_chrome=prefer_chrome,
    )
