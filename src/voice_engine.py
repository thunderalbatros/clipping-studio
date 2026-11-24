"""Commercial-Safe Voice Engine (`human_batch` SM58 + Apache-2.0/MIT TTS adapter, strict KS-4 guard).

Rule 03 Enforcement:
- `voice_mode="none"` (default for CPM Bounties) NEVER synthesizes or overlays AI voice.
- `human_batch` prepares batch Cold Open scripts for Shure SM58 + Mackie Onyx Artist 1.2 and masters
  recorded WAVs to broadcast -14.0 LUFS.
- `tts` strictly permits ONLY verified commercial-safe open-source models (`kokoro-82m` Apache 2.0,
  `pocket-tts` MIT) and rejects non-commercial weights (XTTS-v2, OmniVoice, Fish Audio, CC-BY-NC).
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

COMMERCIAL_TTS_MODELS: dict[str, dict[str, str]] = {
    "kokoro-82m": {"license": "Apache-2.0", "backend": "kokoro_onnx", "status": "approved_commercial"},
    "pocket-tts": {"license": "CC-BY-4.0", "backend": "pocket_tts", "status": "approved_commercial_with_attribution"},
}

BLOCKED_NC_MODELS = (
    "xtts",
    "xtts-v2",
    "omnivoice",
    "k2-fsa/omnivoice",
    "fish-speech",
    "fish-audio",
    "cosyvoice",
    "openaudio-s1",
)


def assert_voice_policy_allowed(voice_mode: str, is_bounty_clip: bool = False, model_name: str | None = None) -> None:
    """Enforce Rule 03 (no AI voice over CPM bounties) and Rule 01 KS-4 (no CC-BY-NC weights)."""
    if voice_mode == "none":
        raise ValueError("voice_mode='none' forbids voiceover generation (Rule 03).")
    if is_bounty_clip and voice_mode == "tts":
        raise ValueError("TTS voiceover is strictly forbidden on CPM bounty clips (Rule 03).")
    if voice_mode == "tts":
        m = (model_name or "").strip().lower()
        if any(b in m for b in BLOCKED_NC_MODELS):
            raise ValueError(f"Kill-Switch KS-4: Non-commercial TTS model blocked: {model_name!r}")
        if m not in COMMERCIAL_TTS_MODELS:
            raise ValueError(
                f"TTS model {model_name!r} is not in commercial-approved registry {sorted(COMMERCIAL_TTS_MODELS)}"
            )


def prepare_human_batch_sheet(
    clips: list[dict[str, str]],
    out_dir: Path,
) -> dict[str, Any]:
    """Generate a Shure SM58 batch recording sheet + JSON manifest for 3-4s Cold Opens."""
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "sm58_recording_sheet.md"
    manifest_path = out_dir / "sm58_batch_manifest.json"

    lines = [
        "# Shure SM58 + Mackie Onyx Artist 1.2 — Human Batch Recording Sheet",
        f"- Generated: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
        "- Target: 48 kHz 24-bit Mono WAV, 15 cm off-axis, peak -12 to -6 dBFS.",
        "",
    ]
    entries: list[dict[str, str]] = []
    for idx, c in enumerate(clips, start=1):
        cid = str(c.get("clip_id") or f"clip_{idx:02d}")
        script = str(c.get("cold_open_script") or c.get("hook_headline") or "").strip()
        target_wav = str(out_dir / f"{cid}_cold_open_raw.wav")
        lines.append(f"## {idx}. `{cid}` -> `{Path(target_wav).name}`")
        lines.append(f"> {script}")
        lines.append("")
        entries.append({"clip_id": cid, "script": script, "target_raw_wav": target_wav})

    md_path.write_text("\n".join(lines), encoding="utf-8")
    manifest = {"generated_at": datetime.now(UTC).isoformat(), "voice_mode": "human_batch", "entries": entries}
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"sheet_md": str(md_path), "manifest_json": str(manifest_path), "count": len(entries)}


def master_voice_wav(raw_wav: Path, out_wav: Path) -> Path:
    """Apply broadcast vocal chain (80Hz HPF + gentle compression + -14 LUFS loudnorm + -1.5 dBTP limiter)."""
    if not raw_wav.exists():
        raise FileNotFoundError(f"Raw voice WAV not found: {raw_wav}")
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(raw_wav),
        "-af",
        "highpass=f=80,acompressor=threshold=-18dB:ratio=3:attack=5:release=60,"
        "loudnorm=I=-14.0:TP=-1.5:LRA=9.0,alimiter=limit=0.82:level=false",
        "-ar",
        "48000",
        "-ac",
        "2",
        str(out_wav),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=60)
    if res.returncode != 0 or not out_wav.exists():
        raise RuntimeError(f"Voice WAV mastering failed: {res.stderr[-300:]}")
    return out_wav


def synthesize_commercial_tts(
    text: str,
    out_wav: Path,
    *,
    model_name: str = "kokoro-82m",
    voice: str = "am_michael",
    is_bounty_clip: bool = False,
) -> dict[str, Any]:
    """Synthesize commercial-safe local TTS (Kokoro-82M Apache 2.0 or Pocket-TTS MIT) if installed."""
    assert_voice_policy_allowed("tts", is_bounty_clip=is_bounty_clip, model_name=model_name)
    out_wav.parent.mkdir(parents=True, exist_ok=True)

    if model_name == "kokoro-82m":
        import importlib

        try:
            kokoro_onnx: Any = importlib.import_module("kokoro_onnx")
            sf: Any = importlib.import_module("soundfile")
        except ImportError as exc:
            raise RuntimeError(
                "kokoro-onnx is not installed in .venv. Install optional commercial TTS via "
                "`uv pip install kokoro-onnx soundfile` and download Apache-2.0 kokoro-v1.0.onnx + voices-v1.0.bin."
            ) from exc
        model_path = Path("models") / "kokoro-v1.0.onnx"
        voices_path = Path("models") / "voices-v1.0.bin"
        if not (model_path.exists() and voices_path.exists()):
            raise FileNotFoundError("Missing models/kokoro-v1.0.onnx or models/voices-v1.0.bin")
        kokoro = kokoro_onnx.Kokoro(str(model_path), str(voices_path))
        samples, sr = kokoro.create(text, voice=voice, speed=1.05, lang="en-us")
        raw = out_wav.with_suffix(".raw.wav")
        sf.write(str(raw), samples, sr)
        master_voice_wav(raw, out_wav)
        raw.unlink(missing_ok=True)
        return {"ok": True, "model": model_name, "license": "Apache-2.0", "out_wav": str(out_wav)}

    raise RuntimeError(f"Backend for {model_name} requires optional package installation.")
