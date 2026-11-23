"""Local CPU int8 ASR (parakeet-tdt-0.6b-v3 via onnx-asr), pure YAMNet ONNX anti-DMCA, and Contact-Sheet PNG (Hito 2.0)."""

from __future__ import annotations

import argparse
import csv
import math
import shutil
import subprocess
import tempfile
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import numpy as np
import onnx_asr
import onnxruntime as ort
import yaml
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

from src.db import upsert_slice, upsert_transcript_fts
from src.miner import CHANNEL_PROFILE_PATH, PROJECT_ROOT, RUNS_DIR, enforce_runs_quota
from src.schemas import (
    AnnotatedTranscript,
    AudioPolicy,
    CandidateSlice,
    NonVerbalEvent,
    RunState,
    WordTimestamp,
)

_MODEL_CACHE: dict[str, Any] = {}
YAMNET_MODEL_PATH = PROJECT_ROOT / "models" / "yamnet" / "yamnet.onnx"
YAMNET_CLASS_MAP_PATH = PROJECT_ROOT / "models" / "yamnet" / "yamnet_class_map.csv"
DMCA_MUSIC_THRESHOLD = 0.45

_MUSIC_CLASS_KEYWORDS = (
    "music",
    "singing",
    "song",
    "rapping",
    "musical instrument",
    "hip hop",
    "pop music",
    "rock music",
    "electronic music",
    "soundtrack",
    "disco",
    "reggae",
    "techno",
    "jazz",
    "funk",
    "soul",
    "choir",
)


def _load_asr_pipeline():
    if "parakeet" not in _MODEL_CACHE:
        vad = onnx_asr.load_vad("silero")
        model = onnx_asr.load_model("nemo-parakeet-tdt-0.6b-v3", quantization="int8", providers=["CPUExecutionProvider"])
        _MODEL_CACHE["parakeet"] = model.with_vad(vad).with_timestamps()
    return _MODEL_CACHE["parakeet"]


def _load_yamnet() -> tuple[ort.InferenceSession, dict[int, str], list[int]] | None:
    if not YAMNET_MODEL_PATH.exists() or not YAMNET_CLASS_MAP_PATH.exists():
        return None
    if "yamnet" not in _MODEL_CACHE:
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        sess = ort.InferenceSession(str(YAMNET_MODEL_PATH), sess_options=opts, providers=["CPUExecutionProvider"])
        class_map: dict[int, str] = {}
        music_ids: list[int] = []
        with YAMNET_CLASS_MAP_PATH.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.reader(fh)
            next(reader, None)
            for row in reader:
                if len(row) >= 3:
                    cid = int(row[0])
                    name = row[2].strip()
                    class_map[cid] = name
                    lname = name.lower()
                    if any(k in lname for k in _MUSIC_CLASS_KEYWORDS):
                        music_ids.append(cid)
        _MODEL_CACHE["yamnet"] = (sess, class_map, music_ids)
    return _MODEL_CACHE["yamnet"]  # type: ignore[return-value]


def _read_wav_mono_16k(wav_path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(wav_path), "rb") as wf:
        sr = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return samples, sr


def _group_segment_words(
    seg_start: float,
    seg_end: float,
    tokens: list[str] | None,
    timestamps: list[float] | None,
    logprobs: list[float] | None,
) -> list[WordTimestamp]:
    if not tokens or not timestamps:
        return []

    raw_words: list[dict[str, Any]] = []
    for idx, tok in enumerate(tokens):
        ts_rel = float(timestamps[idx]) if idx < len(timestamps) else 0.0
        abs_t = round(max(0.0, min(seg_end, seg_start + ts_rel)), 3)
        lp = float(logprobs[idx]) if (logprobs and idx < len(logprobs)) else 0.0
        conf = round(max(0.0, min(1.0, math.exp(lp) if lp < 0 else 1.0)), 3)

        is_new = idx == 0 or tok.startswith(" ")
        clean_piece = tok.strip()
        if not clean_piece:
            continue

        if raw_words and clean_piece in {",", ".", "!", "?", ":", ";", "'", "’"}:
            raw_words[-1]["word"] = str(raw_words[-1]["word"]) + clean_piece
            raw_words[-1]["end"] = max(float(raw_words[-1]["end"]), abs_t + 0.08)
            continue

        if not is_new and raw_words and clean_piece[0].isdigit() and str(raw_words[-1]["word"])[-1].isalpha():
            is_new = True

        if is_new or not raw_words:
            raw_words.append({"word": clean_piece, "start": abs_t, "end": abs_t + 0.12, "confs": [conf]})
        else:
            raw_words[-1]["word"] = str(raw_words[-1]["word"]) + clean_piece
            raw_words[-1]["end"] = max(float(raw_words[-1]["end"]), abs_t + 0.12)
            confs = raw_words[-1]["confs"]
            assert isinstance(confs, list)
            confs.append(conf)

    out: list[WordTimestamp] = []
    for i, rw in enumerate(raw_words):
        s_t = float(rw["start"])
        if i + 1 < len(raw_words):
            next_s = float(raw_words[i + 1]["start"])
            upper_bound = max(s_t + 0.01, next_s - 0.01)
            e_t = round(min(seg_end, float(rw["end"]), upper_bound), 3)
        else:
            e_t = round(max(s_t + 0.04, min(seg_end, float(rw["end"]))), 3)
        confs = rw["confs"]
        assert isinstance(confs, list)
        out.append(
            WordTimestamp(
                word=str(rw["word"]),
                start=s_t,
                end=e_t,
                confidence=round(sum(confs) / max(1, len(confs)), 3),
            )
        )
    return out


def _detect_music_yamnet_onnx(
    samples: np.ndarray,
) -> tuple[float, bool, list[NonVerbalEvent], list[dict[str, float | str]]] | None:
    """Run pure YAMNet ONNX AudioSet classifier (zero TensorFlow) for real music/singing/laughter detection."""
    loaded = _load_yamnet()
    if loaded is None or len(samples) < 16000:
        return None
    sess, class_map, music_ids = loaded
    inp_name = sess.get_inputs()[0].name
    scores = np.asarray(sess.run(None, {inp_name: samples.astype(np.float32)})[0], dtype=np.float32)  # [T, 521]
    if scores.ndim != 2 or scores.shape[0] == 0:
        return None

    hop_sec = 0.48
    music_per_frame = np.max(scores[:, music_ids], axis=1)
    if len(music_per_frame) >= 4:
        smoothed = np.convolve(music_per_frame, np.ones(4, dtype=np.float32) / 4.0, mode="valid")
        music_prob_peak = round(float(np.max(smoothed)), 3)
        events: list[NonVerbalEvent] = []
        for idx, val in enumerate(smoothed):
            if float(val) >= DMCA_MUSIC_THRESHOLD:
                t0 = round(idx * hop_sec, 2)
                events.append(
                    NonVerbalEvent(
                        kind="music_spike",
                        start=t0,
                        end=round(t0 + 1.92, 2),
                        intensity=round(float(val), 3),
                        note="[yamnet_music_alert]",
                    )
                )
    else:
        music_prob_peak = round(float(np.max(music_per_frame)), 3)
        events = []

    # Also emit laughter peaks from YAMNet AudioSet classes (Laughter=13, Giggle=14, Chuckle=16)
    laugh_ids = [cid for cid, name in class_map.items() if "laughter" in name.lower() or "chuckle" in name.lower()]
    if laugh_ids:
        laugh_scores = np.max(scores[:, laugh_ids], axis=1)
        for idx, val in enumerate(laugh_scores):
            if float(val) >= 0.35:
                t0 = round(idx * hop_sec, 2)
                events.append(
                    NonVerbalEvent(
                        kind="laughter",
                        start=t0,
                        end=round(t0 + 0.96, 2),
                        intensity=round(float(val), 3),
                        note="[yamnet_laughter]",
                    )
                )

    mean_scores = np.mean(scores, axis=0)
    peak_scores = np.max(scores, axis=0)
    top_ids = np.argsort(mean_scores)[::-1][:6]
    top_events: list[dict[str, float | str]] = [
        {
            "class": class_map.get(int(cid), str(cid)),
            "mean": round(float(mean_scores[cid]), 3),
            "peak": round(float(peak_scores[cid]), 3),
        }
        for cid in top_ids
    ]

    dmca_flag = music_prob_peak >= DMCA_MUSIC_THRESHOLD
    return music_prob_peak, dmca_flag, events, top_events


def _detect_music_fft_fallback(
    samples: np.ndarray,
    sr: int,
    words: list[WordTimestamp],
) -> tuple[float, bool, list[NonVerbalEvent]]:
    """Fallback numpy FFT heuristic used ONLY when models/yamnet/yamnet.onnx is unavailable."""
    events: list[NonVerbalEvent] = []
    speech_intervals = [(w.start, w.end) for w in words]
    m_win = int(sr * 1.0)
    m_hop = int(sr * 0.5)
    freqs = np.fft.rfftfreq(m_win, d=1.0 / sr)
    band_mask = (freqs >= 80.0) & (freqs <= 4000.0)
    hann = np.hanning(m_win).astype(np.float32)

    music_scores: list[tuple[float, float]] = []
    for pos in range(0, max(1, len(samples) - m_win + 1), m_hop):
        t0 = pos / float(sr)
        t1 = t0 + 1.0
        seg = samples[pos : pos + m_win]
        rms_db = 20.0 * math.log10(float(np.sqrt(np.mean(seg * seg) + 1e-12)) + 1e-9)
        if rms_db < -38.0:
            music_scores.append((t0, 0.0))
            continue
        spec = np.abs(np.fft.rfft(seg * hann)) + 1e-9
        band = spec[band_mask]
        flatness = min(1.0, float(np.exp(np.mean(np.log(band)))) / (float(np.mean(band)) + 1e-9))
        tonality = max(0.0, 1.0 - flatness * 3.0)
        sub = seg.reshape(10, -1)
        sub_rms = np.sqrt(np.mean(sub * sub, axis=1) + 1e-12)
        cv = float(np.std(sub_rms) / (np.mean(sub_rms) + 1e-9))
        sustained = max(0.0, min(1.0, 1.0 - (cv / 0.85)))
        in_speech = any(not (t1 <= ws or t0 >= we) for ws, we in speech_intervals)
        bg_boost = 0.20 if (not in_speech and rms_db > -34.0) else 0.0
        prob = min(1.0, max(0.0, 0.45 * tonality + 0.40 * sustained + bg_boost))
        music_scores.append((t0, prob))

    if len(music_scores) >= 4:
        vals = [p for _, p in music_scores]
        smoothed = [float(np.mean(vals[i : i + 4])) for i in range(len(vals) - 3)]
        music_prob_peak = round(max(smoothed), 3)
        for i, avg_p in enumerate(smoothed):
            if avg_p >= DMCA_MUSIC_THRESHOLD:
                events.append(
                    NonVerbalEvent(
                        kind="music_spike",
                        start=round(music_scores[i][0], 2),
                        end=round(music_scores[i][0] + 2.0, 2),
                        intensity=round(avg_p, 3),
                        note="[fft_music_fallback]",
                    )
                )
    else:
        music_prob_peak = round(max((p for _, p in music_scores), default=0.0), 3)
    return music_prob_peak, music_prob_peak >= DMCA_MUSIC_THRESHOLD, events


def _analyze_acoustics_and_dmca(
    samples: np.ndarray,
    sr: int,
    words: list[WordTimestamp],
) -> tuple[list[NonVerbalEvent], float, bool, list[dict[str, float | str]], Literal["yamnet_onnx", "fft_fallback"]]:
    """Detect shouting, silences, and real AudioSet music risk using pure YAMNet ONNX."""
    events: list[NonVerbalEvent] = []
    if len(samples) == 0 or sr <= 0:
        return events, 0.0, False, [], "yamnet_onnx"

    hop = int(sr * 0.25)
    win = int(sr * 0.50)
    rms_dbs: list[tuple[float, float]] = []
    for pos in range(0, max(1, len(samples) - win + 1), hop):
        seg = samples[pos : pos + win]
        rms = float(np.sqrt(np.mean(seg * seg) + 1e-12))
        db = 20.0 * math.log10(rms + 1e-9)
        rms_dbs.append((pos / float(sr), db))

    speech_dbs = [db for _, db in rms_dbs if db > -40.0]
    med_db = float(np.median(speech_dbs)) if speech_dbs else -24.0
    shout_thresh = max(med_db + 8.0, -16.0)

    in_shout = False
    shout_start = 0.0
    shout_peak = 0.0
    for t, db in rms_dbs:
        if db >= shout_thresh:
            if not in_shout:
                in_shout = True
                shout_start = t
                shout_peak = db
            else:
                shout_peak = max(shout_peak, db)
        elif in_shout:
            events.append(
                NonVerbalEvent(
                    kind="shouting",
                    start=round(shout_start, 2),
                    end=round(t + 0.25, 2),
                    intensity=round(max(0.0, shout_peak + 60.0), 2),
                    note="[shouting]",
                )
            )
            in_shout = False
    if in_shout and rms_dbs:
        events.append(
            NonVerbalEvent(
                kind="shouting",
                start=round(shout_start, 2),
                end=round(rms_dbs[-1][0] + 0.5, 2),
                intensity=round(max(0.0, shout_peak + 60.0), 2),
                note="[shouting]",
            )
        )

    for idx in range(len(words) - 1):
        gap = words[idx + 1].start - words[idx].end
        if gap >= 0.50:
            s_idx = int(words[idx].end * sr)
            e_idx = int(words[idx + 1].start * sr)
            seg = samples[s_idx:e_idx]
            if len(seg) > 0:
                rms_db = 20.0 * math.log10(float(np.sqrt(np.mean(seg * seg) + 1e-12)) + 1e-9)
                if rms_db < -38.0:
                    events.append(
                        NonVerbalEvent(
                            kind="silence",
                            start=round(words[idx].end, 2),
                            end=round(words[idx + 1].start, 2),
                            intensity=round(abs(rms_db), 2),
                            note=f"[silence {gap:.2f}s]",
                        )
                    )

    yam_res = _detect_music_yamnet_onnx(samples)
    if yam_res is not None:
        music_prob_peak, dmca_flag, yam_events, top_events = yam_res
        events.extend(yam_events)
        return events, music_prob_peak, dmca_flag, top_events, "yamnet_onnx"

    music_prob_peak, dmca_flag, fft_events = _detect_music_fft_fallback(samples, sr, words)
    events.extend(fft_events)
    return events, music_prob_peak, dmca_flag, [], "fft_fallback"


def generate_contact_sheet(video_path: Path, out_png: Path, interval_sec: float = 10.0) -> Path:
    """Extract 1 frame every 10s (or evenly across clip) and pack into a 4x2 labeled PNG contact-sheet."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(video_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    dur = float(probe.stdout.strip() or "0")
    step = interval_sec if dur >= 45.0 else max(2.0, round(dur / 8.0, 2))
    timestamps = [round(t, 1) for t in np.arange(0.0, max(0.5, dur - 0.2), step)][:8]
    if not timestamps:
        timestamps = [0.0]

    tmp_dir = Path(tempfile.mkdtemp(prefix="cs_sheet_"))
    tile_w, tile_h = 320, 180
    cols, rows = 4, 2
    sheet = Image.new("RGB", (cols * tile_w, rows * tile_h), color=(12, 14, 18))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()

    try:
        for idx, ts in enumerate(timestamps):
            frame_png = tmp_dir / f"f_{idx:02d}.png"
            subprocess.run(
                [
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
                    "-vf",
                    f"scale={tile_w}:{tile_h}:force_original_aspect_ratio=decrease,pad={tile_w}:{tile_h}:(ow-iw)/2:(oh-ih)/2",
                    str(frame_png),
                ],
                check=True,
            )
            tile = Image.open(frame_png).convert("RGB")
            gx = (idx % cols) * tile_w
            gy = (idx // cols) * tile_h
            sheet.paste(tile, (gx, gy))
            draw.rectangle([gx + 6, gy + 6, gx + 88, gy + 24], fill=(0, 0, 0))
            draw.text((gx + 10, gy + 9), f"t={ts:.1f}s", fill=(255, 230, 60), font=font)

        out_png.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(out_png, format="PNG")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return out_png


def evaluate_audio_policy_yamnet(
    music_prob_peak: float,
    audio_policy: AudioPolicy = "strict_clean",
    threshold: float = DMCA_MUSIC_THRESHOLD,
) -> tuple[bool, bool, str]:
    """Evaluate YAMNet music probability under 3-mode policy -> `(dmca_flag, needs_demucs_rescue, dmca_advisory)`."""
    has_music = float(music_prob_peak) >= threshold
    if audio_policy == "keep_original":
        advisory = (
            f"keep_original_advisory: YAMNet music_prob_peak={music_prob_peak:.3f} (>={threshold}) "
            "retained with -14 LUFS loudnorm (no rejection)"
            if has_music
            else f"keep_original: clean audio (music_prob_peak={music_prob_peak:.3f})"
        )
        return False, False, advisory
    if audio_policy == "sound_campaign":
        if has_music:
            return (
                True,
                True,
                f"sound_campaign: foreign background music detected (peak={music_prob_peak:.3f}) — "
                "clean audio required to sync Official Sound in-app",
            )
        return False, False, "sound_campaign: clean audio verified; ready for Official Sound in-app sync at climax"
    # strict_clean (default for 16:9 YPP / multi-platform safe distribution)
    if has_music:
        return (
            True,
            True,
            f"strict_clean: licensed music detected (peak={music_prob_peak:.3f} >= {threshold}) — "
            "avoid window or queue Carril 2 Colab T4 Demucs rescue",
        )
    return False, False, ""


def _estimate_visual_climax_sec(events: list[NonVerbalEvent], duration_sec: float) -> float:
    """Estimate peak impact timestamp (seconds) from shouting/laughter peaks for Sound Campaign sync."""
    peaks = [ev for ev in events if ev.kind in {"shouting", "laughter", "visual_motion"}]
    if peaks:
        best = max(peaks, key=lambda e: float(e.intensity))
        return round(max(0.5, min(max(0.5, duration_sec - 0.5), (best.start + best.end) * 0.5)), 2)
    return round(max(0.5, duration_sec * 0.45), 2)


def transcribe_slice(
    candidate: CandidateSlice,
    run_dir: Path,
    auto_cleanup_temp: bool = True,
    audio_policy: AudioPolicy = "strict_clean",
) -> AnnotatedTranscript:
    """Run CPU int8 Parakeet + Silero VAD, pure YAMNet ONNX 3-mode audio policy, and Contact-Sheet PNG."""
    audio_path = Path(candidate.slice_audio_path) if candidate.slice_audio_path else None
    video_path = Path(candidate.slice_video_path)
    if audio_path is None or not audio_path.exists():
        audio_path = run_dir / f"{candidate.slice_id}_16k.wav"
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
                str(audio_path),
            ],
            check=True,
        )

    pipeline = _load_asr_pipeline()
    segments = list(pipeline.recognize(str(audio_path)))

    all_words: list[WordTimestamp] = []
    texts: list[str] = []
    for seg in segments:
        if seg.text.strip():
            texts.append(seg.text.strip())
        all_words.extend(_group_segment_words(seg.start, seg.end, seg.tokens, seg.timestamps, seg.logprobs))

    samples, sr = _read_wav_mono_16k(audio_path)
    dur_sec = round(len(samples) / float(sr), 2) if sr else candidate.duration_sec
    events, music_prob_peak, _raw_dmca_flag, top_events, detector = _analyze_acoustics_and_dmca(samples, sr, all_words)
    dmca_flag, needs_demucs, dmca_advisory = evaluate_audio_policy_yamnet(music_prob_peak, audio_policy=audio_policy)
    climax_sec = _estimate_visual_climax_sec(events, dur_sec)

    sheet_path = run_dir / f"{candidate.slice_id}_contact_sheet.png"
    generate_contact_sheet(video_path, sheet_path, interval_sec=10.0)

    transcript = AnnotatedTranscript(
        slice_id=candidate.slice_id,
        language="en",
        full_text=" ".join(texts),
        words=all_words,
        non_verbal_events=events,
        music_prob_peak=music_prob_peak,
        dmca_flag=dmca_flag,
        yamnet_top_events=top_events,
        dmca_detector=detector,
        audio_policy=audio_policy,
        dmca_advisory=dmca_advisory,
        needs_demucs_rescue=needs_demucs,
        visual_climax_sec=climax_sec,
        contact_sheet_path=str(sheet_path),
        duration_sec=dur_sec,
    )
    (run_dir / f"{candidate.slice_id}_transcript.json").write_text(transcript.model_dump_json(indent=2), encoding="utf-8")

    if auto_cleanup_temp and audio_path.exists():
        audio_path.unlink(missing_ok=True)
        candidate.slice_audio_path = None

    return transcript


def transcribe_run(run_dir: Path, audio_policy: AudioPolicy | None = None) -> RunState:
    """Transcribe all mined slices in a run directory, update state.json and studio.db FTS5."""
    load_dotenv(override=True)
    state_path = run_dir / "state.json"
    state = RunState.model_validate_json(state_path.read_text(encoding="utf-8"))
    eff_policy: AudioPolicy = audio_policy or state.audio_policy or "strict_clean"
    state.audio_policy = eff_policy

    auto_cleanup = True
    if CHANNEL_PROFILE_PATH.exists():
        cfg = yaml.safe_load(CHANNEL_PROFILE_PATH.read_text(encoding="utf-8")) or {}
        auto_cleanup = bool(cfg.get("storage", {}).get("auto_cleanup_temp", True))

    transcripts: list[AnnotatedTranscript] = []
    for cand in state.candidates:
        tr = transcribe_slice(cand, run_dir, auto_cleanup_temp=auto_cleanup, audio_policy=eff_policy)
        transcripts.append(tr)
        upsert_slice(
            run_id=state.run_id,
            slice_id=cand.slice_id,
            source_url=cand.source_url,
            platform=cand.platform,
            start_sec=cand.start_sec,
            end_sec=cand.end_sec,
            signal_source=cand.signal_source,
            score=cand.score,
            video_path=cand.slice_video_path,
            source_width=cand.source_width,
            source_height=cand.source_height,
            source_fps=cand.source_fps,
            music_prob_peak=tr.music_prob_peak,
            dmca_flag=tr.dmca_flag,
            dmca_detector=tr.dmca_detector,
        )
        upsert_transcript_fts(state.run_id, cand.slice_id, tr.full_text)

    state.transcripts = transcripts
    state.stage = "transcribed"
    state.disk_usage_mb = enforce_runs_quota(RUNS_DIR, protect_dir=run_dir)
    state.updated_at = datetime.now(UTC).isoformat()
    state_path.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description="Clipping Studio CPU int8 Transcriber + YAMNet ONNX")
    parser.add_argument("--run-id", required=True, help="Run ID inside runs/")
    args = parser.parse_args()

    state = transcribe_run(RUNS_DIR / args.run_id)
    print(f"[TRANSCRIBER] run_id={state.run_id} transcripts={len(state.transcripts)} disk_mb={state.disk_usage_mb}")
    for tr in state.transcripts:
        print(
            f"  - {tr.slice_id}: detector={tr.dmca_detector} words={len(tr.words)} "
            f"music_prob_peak={tr.music_prob_peak} dmca_flag={tr.dmca_flag} top={tr.yamnet_top_events[:3]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
