"""Binary Media Runtime Evals harness (Hito 1.1).

Validates rendered 9:16 clips against 4 mandatory runtime checks:
1. ffprobe_spec: 1080x1920, h264, yuv420p, audio stream present, A/V duration sync <= 0.30s.
2. opening_integrity_1_5s: no black or frozen frames in the first 1.5s.
3. loudness_ebur128: integrated loudness -14.0 LUFS (+/- 2.5 LU) and true peak <= -0.8 dBTP.
4. qa_frames_png: 4 PNG frames extracted at 10%, 35%, 65%, 85% (> 5 KB each).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

_BLACK_START_RE = re.compile(r"black_start:(\d+(?:\.\d+)?)")
_FREEZE_START_RE = re.compile(r"freeze_start:\s*(\d+(?:\.\d+)?)")
_LUFS_I_RE = re.compile(r"Integrated loudness:\s*\r?\n\s*I:\s*(-?\d+(?:\.\d+)?)\s*LUFS")
_PEAK_RE = re.compile(r"True peak:\s*\r?\n\s*Peak:\s*(-?\d+(?:\.\d+)?)\s*dBFS")


def _run_cmd(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")


def check_ffprobe_spec(video_path: Path) -> dict[str, Any]:
    """Check 1080x1920, h264, yuv420p, audio stream, and A/V sync."""
    res = _run_cmd([
        "ffprobe",
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        str(video_path),
    ])
    if res.returncode != 0:
        return {"passed": False, "error": res.stderr.strip()[:200]}

    data = json.loads(res.stdout or "{}")
    streams = data.get("streams", [])
    v_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    a_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not v_stream or not a_stream:
        return {"passed": False, "error": "Missing video or audio stream", "has_video": bool(v_stream), "has_audio": bool(a_stream)}

    width = int(v_stream.get("width", 0))
    height = int(v_stream.get("height", 0))
    codec = v_stream.get("codec_name")
    pix_fmt = v_stream.get("pix_fmt")
    fmt_dur = float(data.get("format", {}).get("duration") or 0.0)
    v_dur = float(v_stream.get("duration") or fmt_dur)
    a_dur = float(a_stream.get("duration") or fmt_dur)
    av_diff = abs(v_dur - a_dur)

    passed = (
        width == 1080
        and height == 1920
        and codec == "h264"
        and pix_fmt == "yuv420p"
        and fmt_dur > 0.5
        and av_diff <= 0.30
    )
    return {
        "passed": passed,
        "width": width,
        "height": height,
        "codec": codec,
        "pix_fmt": pix_fmt,
        "duration_sec": round(fmt_dur, 3),
        "av_diff_sec": round(av_diff, 3),
    }


def check_opening_integrity(video_path: Path, allow_freeze_start: bool = False) -> dict[str, Any]:
    """Check first 1.5s for black frames or unintentional frozen frames."""
    res = _run_cmd([
        "ffmpeg",
        "-hide_banner",
        "-t",
        "1.5",
        "-i",
        str(video_path),
        "-vf",
        "blackdetect=d=0.15:pix_th=0.10,freezedetect=n=0.003:d=0.45",
        "-f",
        "null",
        "-",
    ])
    stderr = res.stderr or ""
    black_starts = [float(m.group(1)) for m in _BLACK_START_RE.finditer(stderr) if float(m.group(1)) < 1.5]
    freeze_starts = [float(m.group(1)) for m in _FREEZE_START_RE.finditer(stderr) if float(m.group(1)) < 0.5]

    has_black = len(black_starts) > 0
    has_freeze = (len(freeze_starts) > 0) and not allow_freeze_start
    passed = (res.returncode == 0) and not has_black and not has_freeze
    return {
        "passed": passed,
        "black_starts": black_starts,
        "freeze_starts": freeze_starts,
        "allow_freeze_start": allow_freeze_start,
    }


def check_loudness_ebur128(video_path: Path) -> dict[str, Any]:
    """Check integrated loudness (-14.0 +/- 2.5 LUFS) and true peak (<= -1.0 dBTP)."""
    res = _run_cmd([
        "ffmpeg",
        "-hide_banner",
        "-i",
        str(video_path),
        "-af",
        "ebur128=peak=true",
        "-f",
        "null",
        "-",
    ])
    stderr = res.stderr or ""
    m_i = _LUFS_I_RE.search(stderr)
    m_p = _PEAK_RE.search(stderr)
    if not m_i or not m_p:
        return {"passed": False, "error": "Could not parse ebur128 summary"}

    lufs = float(m_i.group(1))
    peak = float(m_p.group(1))
    passed = (-16.5 <= lufs <= -11.5) and (peak <= -1.0)
    return {
        "passed": passed,
        "integrated_lufs": lufs,
        "true_peak_dbtp": peak,
    }


def draw_safezones_overlay(frame_png: Path, out_png: Path) -> Path:
    """Draw 2026 double-layer safe-zones (Amber TikTok/Reels + Red Shorts/Universal) with Pillow."""
    with Image.open(frame_png) as _im:
        base = _im.convert("RGBA")
    w, h = base.size

    # Capa Ambar (TikTok / Reels): top 180px, right 150px, bottom 400px.
    amber_top = round(180 * (h / 1920.0))
    amber_right = round(150 * (w / 1080.0))
    amber_bot = round(400 * (h / 1920.0))
    amber_mask = Image.new("L", (w, h), 0)
    adraw = ImageDraw.Draw(amber_mask)
    adraw.rectangle([0, 0, w, amber_top], fill=255)
    adraw.rectangle([w - amber_right, 0, w, h], fill=255)
    adraw.rectangle([0, h - amber_bot, w, h], fill=255)
    amber_layer = Image.composite(
        Image.new("RGBA", (w, h), (255, 176, 0, 68)),
        Image.new("RGBA", (w, h), (0, 0, 0, 0)),
        amber_mask,
    )

    # Capa Roja (YouTube Shorts / Universal 3 redes): top 288px, right 192px, bottom 672px, left 48px.
    red_top = round(288 * (h / 1920.0))
    red_right = round(192 * (w / 1080.0))
    red_bot = round(672 * (h / 1920.0))
    red_left = round(48 * (w / 1080.0))
    safe_box = [red_left, red_top, w - red_right, h - red_bot]

    red_mask = Image.new("L", (w, h), 0)
    rdraw = ImageDraw.Draw(red_mask)
    rdraw.rectangle([0, 0, w, safe_box[1]], fill=255)
    rdraw.rectangle([0, safe_box[3], w, h], fill=255)
    rdraw.rectangle([0, safe_box[1], safe_box[0], safe_box[3]], fill=255)
    rdraw.rectangle([safe_box[2], safe_box[1], w, safe_box[3]], fill=255)
    red_layer = Image.composite(
        Image.new("RGBA", (w, h), (220, 38, 38, 82)),
        Image.new("RGBA", (w, h), (0, 0, 0, 0)),
        red_mask,
    )

    composed = Image.alpha_composite(base, amber_layer)
    composed = Image.alpha_composite(composed, red_layer)
    draw = ImageDraw.Draw(composed)
    draw.rectangle([0, amber_top, w - amber_right, h - amber_bot], outline=(255, 195, 0, 225), width=2)
    draw.rectangle(safe_box, outline=(255, 65, 65, 240), width=4)

    font_path = Path(r"C:\Windows\Fonts\seguibl.ttf")
    try:
        font = ImageFont.truetype(str(font_path), 24) if font_path.exists() else ImageFont.load_default()
    except OSError:
        font = ImageFont.load_default()
    draw.rectangle([28, 24, w - 28, 76], fill=(10, 12, 16, 210))
    draw.text(
        (42, 36),
        "AMBER: TikTok/Reels | RED SAFE BOX [48..888, 288..1248]: 3-Network",
        fill=(255, 240, 200, 255),
        font=font,
    )

    out_png.parent.mkdir(parents=True, exist_ok=True)
    composed.convert("RGB").save(out_png, format="PNG")
    return out_png


def extract_qa_frames_png(video_path: Path, duration_sec: float, qa_dir: Path) -> dict[str, Any]:
    """Extract 4 lossless PNG frames at 10%, 35%, 65%, 85% and render *_safezones.png overlays."""
    qa_dir.mkdir(parents=True, exist_ok=True)
    fractions = [0.10, 0.35, 0.65, 0.85]
    files: list[str] = []
    safezone_files: list[str] = []
    sizes: list[int] = []
    for idx, frac in enumerate(fractions, start=1):
        ts = max(0.05, round(duration_sec * frac, 3))
        out_png = qa_dir / f"qa_frame_{idx}.png"
        sz_png = qa_dir / f"qa_frame_{idx}_safezones.png"
        res = _run_cmd([
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            str(ts),
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            str(out_png),
        ])
        if res.returncode != 0 or not out_png.exists():
            return {"passed": False, "error": f"Failed extracting frame {idx}", "files": files}
        draw_safezones_overlay(out_png, sz_png)
        sz = out_png.stat().st_size
        files.append(str(out_png))
        safezone_files.append(str(sz_png))
        sizes.append(sz)

    passed = len(files) == 4 and len(safezone_files) == 4 and all(sz > 5120 for sz in sizes)
    return {
        "passed": passed,
        "files": files,
        "safezone_files": safezone_files,
        "sizes_bytes": sizes,
    }


def evaluate_video(video_path: Path, qa_dir: Path | None = None, allow_freeze_start: bool = False) -> dict[str, Any]:
    """Run all 4 binary Media Runtime Evals and return structured verdict."""
    if not video_path.exists():
        return {"passed": False, "error": f"File not found: {video_path}"}

    target_qa_dir = qa_dir or (video_path.parent / f"{video_path.stem}_qa_frames")
    c1 = check_ffprobe_spec(video_path)
    c2 = check_opening_integrity(video_path, allow_freeze_start=allow_freeze_start)
    c3 = check_loudness_ebur128(video_path)
    dur = float(c1.get("duration_sec") or 1.0)
    c4 = extract_qa_frames_png(video_path, dur, target_qa_dir)

    all_passed = bool(c1["passed"] and c2["passed"] and c3["passed"] and c4["passed"])
    return {
        "video": str(video_path),
        "passed": all_passed,
        "checks": {
            "ffprobe_spec": c1,
            "opening_integrity_1_5s": c2,
            "loudness_ebur128": c3,
            "qa_frames_png": c4,
        },
    }


def _encode_selftest_clip(out_path: Path, video_filter: str) -> None:
    """Build synthetic 1080x1920 2.5s clip with -14 LUFS audio via h264_qsv (fallback libx264)."""
    base_args = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=1080x1920:rate=30:duration=2.5",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000:duration=2.5",
        "-filter_complex",
        f"{video_filter};[1:a]loudnorm=I=-14:TP=-1.5:LRA=11[a]",
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-t",
        "2.5",
    ]
    qsv = _run_cmd(base_args + ["-c:v", "h264_qsv", "-global_quality", "24", "-c:a", "aac", "-b:a", "128k", str(out_path)])
    if qsv.returncode != 0:
        _run_cmd(base_args + ["-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-b:a", "128k", str(out_path)])


def _run_self_test() -> int:
    tmp_dir = Path(tempfile.mkdtemp(prefix="cs_media_eval_"))
    try:
        valid_mp4 = tmp_dir / "valid_1080x1920.mp4"
        broken_mp4 = tmp_dir / "broken_black_start.mp4"

        _encode_selftest_clip(valid_mp4, "[0:v]format=nv12[v]")
        _encode_selftest_clip(
            broken_mp4,
            "color=c=black:s=1080x1920:r=30:d=0.8[blk];[0:v]trim=duration=1.7,setpts=PTS-STARTPTS[src];[blk][src]concat=n=2:v=1:a=0,format=nv12[v]",
        )

        res_valid = evaluate_video(valid_mp4, qa_dir=tmp_dir / "qa_valid")
        res_broken = evaluate_video(broken_mp4, qa_dir=tmp_dir / "qa_broken")

        print("=== SELF-TEST VALID CLIP (EXPECT PASS) ===")
        print(json.dumps(res_valid, indent=2))
        print("=== SELF-TEST BLACK-START CLIP (EXPECT FAIL) ===")
        print(json.dumps(res_broken, indent=2))

        ok_valid = res_valid["passed"] is True
        ok_broken = (
            res_broken["passed"] is False
            and res_broken["checks"]["opening_integrity_1_5s"]["passed"] is False
        )
        print(f"SELF_TEST_SUMMARY: valid_pass={ok_valid} broken_fail_caught={ok_broken}")
        return 0 if (ok_valid and ok_broken) else 1
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Clipping Studio Binary Media Runtime Evals")
    parser.add_argument("video", nargs="?", help="Path to rendered MP4 clip")
    parser.add_argument("--qa-dir", help="Directory for extracted QA PNG frames")
    parser.add_argument("--allow-freeze-start", action="store_true", help="Allow intentional freeze frame at clip start")
    parser.add_argument("--self-test", action="store_true", help="Run PASS/FAIL synthetic self-test")
    args = parser.parse_args()

    if args.self_test:
        return _run_self_test()
    if not args.video:
        parser.print_help()
        return 1

    result = evaluate_video(
        Path(args.video),
        qa_dir=Path(args.qa_dir) if args.qa_dir else None,
        allow_freeze_start=args.allow_freeze_start,
    )
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
