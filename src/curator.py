"""Multimodal 2-pass Curator, LR-ASD streamer-only guard, anti-meta-talk gate, brief matcher, and ClipPackaging (Hito 2.0)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.db import upsert_slice, upsert_transcript_fts
from src.llm_router import build_cached_system_instruction, generate_structured
from src.miner import CHANNEL_PROFILE_PATH, PROJECT_ROOT, STREAMERS_PATH
from src.schemas import (
    AnnotatedTranscript,
    BlindCriticResult,
    CandidateSlice,
    ClipPackaging,
    EditPlan,
    FreezeFrameSpec,
    RunState,
    SliceAuditResult,
)
from src.speaker_guard import score_streamer_speech_asd

LORE_INDEX_PATH = PROJECT_ROOT / "context" / "gta6_lore_index.json"
_WEAK_OPENERS = {"um", "uh", "uhh", "umm", "so", "and", "but", "or", "because", "then"}
_META_TALK_PATTERNS = (
    "gifted",
    "thank you for the sub",
    "thanks for the sub",
    "prime sub",
    "appreciate the sub",
    "welcome in",
    "shoutout to",
    "raided",
    "bits",
    "donation",
    "months in a row",
    "thank you for the other",
    "gifting",
)


class _Pass2TrimProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clip_id: str
    start_trim: float = Field(ge=0.0)
    end_trim: float = Field(gt=0.0)
    hook_headline: str
    freeze_frame: FreezeFrameSpec | None = None
    matched_trend_id: str | None = None
    matched_lore_entity: str | None = None
    dmca_clear: bool = True
    dmca_risk_reason: str = ""
    self_contained_score: int = Field(ge=0, le=100)
    rationale: str


def _probe_video_geometry(video_path: Path) -> tuple[int, int, float]:
    res = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height:format=duration",
            "-of",
            "json",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        return 1920, 1080, 75.0
    data = json.loads(res.stdout or "{}")
    st = (data.get("streams") or [{}])[0]
    w = int(st.get("width") or 1920)
    h = int(st.get("height") or 1080)
    dur = float((data.get("format") or {}).get("duration") or 75.0)
    return w, h, dur


def _unscale_bbox_2d(box_2d: tuple[int, int, int, int] | None, width: int, height: int) -> tuple[int, int, int, int] | None:
    if not box_2d or len(box_2d) != 4:
        return None
    ymin, xmin, ymax, xmax = box_2d
    ymin, ymax = max(0, min(1000, ymin)), max(0, min(1000, ymax))
    xmin, xmax = max(0, min(1000, xmin)), max(0, min(1000, xmax))
    if xmax - xmin < 60 or ymax - ymin < 60:
        return None
    x = round((xmin / 1000.0) * width) & ~1
    y = round((ymin / 1000.0) * height) & ~1
    bw = round(((xmax - xmin) / 1000.0) * width) & ~1
    bh = round(((ymax - ymin) / 1000.0) * height) & ~1
    if bw < 64 or bh < 64:
        return None
    if bw > int(width * 0.55) or bh > int(height * 0.60):
        return None
    return (min(max(0, x), width - 64), min(max(0, y), height - 64), min(bw, width - x), min(bh, height - y))


def _load_streamer_webcam_preset(streamer_id: str) -> tuple[int, int, int, int] | None:
    if not STREAMERS_PATH.exists():
        return None
    data = yaml.safe_load(STREAMERS_PATH.read_text(encoding="utf-8")) or {}
    entry = (data.get("streamers") or {}).get(streamer_id) or {}
    wb = entry.get("webcam_box")
    if isinstance(wb, list) and len(wb) == 4:
        return (int(wb[0]), int(wb[1]), int(wb[2]), int(wb[3]))
    return None


def _match_brief_context(brief: str | None) -> str:
    if not brief:
        return ""
    if not LORE_INDEX_PATH.exists():
        return f"CAMPAIGN BRIEF REQUIREMENT: {brief}\n"
    lore = json.loads(LORE_INDEX_PATH.read_text(encoding="utf-8"))
    triggers: dict[str, Any] = lore.get("brief_triggers") or {}
    bl = brief.lower()
    matched_blocks: list[str] = []
    for key, spec in triggers.items():
        if key.replace("_", " ") in bl or any(tok in bl for tok in key.split("_")):
            matched_blocks.append(f"{key}: keywords={spec.get('keywords_transcript')}, cues={spec.get('visual_cues')}")
    extra = f" Matched lore triggers: {'; '.join(matched_blocks)}" if matched_blocks else ""
    return f"CAMPAIGN BRIEF REQUIREMENT: {brief}.{extra}\n"


def _extract_zoom_strip_1fps(video_path: Path, start_sec: float, duration_sec: float, out_png: Path) -> Path:
    """Extract an 8-frame temporal contact strip spanning the full [start_sec .. start_sec+duration_sec] arc."""
    span = max(8.0, duration_sec)
    step = span / 8.0
    timestamps = [round(start_sec + (i + 0.35) * step, 2) for i in range(8)]
    tile_w, tile_h = 320, 180
    sheet = Image.new("RGB", (4 * tile_w, 2 * tile_h), (12, 14, 18))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    with tempfile.TemporaryDirectory(prefix="cs_zoom1fps_") as tmp:
        for idx, ts in enumerate(timestamps):
            fp = Path(tmp) / f"z_{idx}.png"
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
                    str(fp),
                ],
                check=False,
            )
            if fp.exists():
                tile = Image.open(fp).convert("RGB")
                gx, gy = (idx % 4) * tile_w, (idx // 4) * tile_h
                sheet.paste(tile, (gx, gy))
                draw.rectangle([gx + 6, gy + 6, gx + 96, gy + 24], fill=(0, 0, 0))
                draw.text((gx + 10, gy + 9), f"t={ts:.1f}s", fill=(0, 229, 255), font=font)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_png, format="PNG")
    return out_png


def _apply_pass1_hard_gates(audit: SliceAuditResult, transcript: AnnotatedTranscript) -> SliceAuditResult:
    if audit.scene_type != "active_gameplay":
        audit.approved_for_pass2 = False
        if transcript.dmca_flag:
            audit.dmca_clear = False
        if not audit.rationale:
            audit.rationale = f"Rejected: scene_type={audit.scene_type} != active_gameplay."

    sug_dur = max(0.0, audit.suggested_end_sec - audit.suggested_start_sec)
    if transcript.dmca_flag and (not audit.dmca_clear or sug_dur > 60.0):
        audit.dmca_clear = False
        audit.approved_for_pass2 = False
        audit.dmca_risk_reason = (
            audit.dmca_risk_reason
            or f"Rejected by YAMNet anti-DMCA gate: music_prob_peak={transcript.music_prob_peak} (>={0.45})"
        )
    if not audit.dmca_clear:
        audit.approved_for_pass2 = False
    elif (
        audit.scene_type == "active_gameplay"
        and audit.self_contained_score >= 55
        and audit.visual_hook_score >= 0.45
    ):
        audit.approved_for_pass2 = True
    return audit


def _extract_single_ref_frame(video_path: Path, timestamp_sec: float, out_png: Path) -> Path | None:
    """Extract a single 16:9 reference frame (960x540) for exact 0..1000 webcam_box_2d localization."""
    out_png.parent.mkdir(parents=True, exist_ok=True)
    res = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.5, timestamp_sec):.2f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-vf",
            "scale=960:540",
            str(out_png),
        ],
        capture_output=True,
        check=False,
    )
    return out_png if (res.returncode == 0 and out_png.exists()) else None


def audit_slice_pass1(
    cand: CandidateSlice,
    transcript: AnnotatedTranscript,
    streamer_id: str,
    brief: str | None = None,
) -> SliceAuditResult:
    """Pass 1: Multimodal audit (scene_type, webcam bbox on single 16:9 ref frame, YAMNet DMCA, GTA VI lore)."""
    video_path = Path(cand.slice_video_path)
    vw, vh, vdur = _probe_video_geometry(video_path)
    sheet_path = Path(transcript.contact_sheet_path) if transcript.contact_sheet_path else video_path.with_name(f"{cand.slice_id}_contact_sheet.png")
    ref_frame_path = _extract_single_ref_frame(video_path, min(max(2.0, vdur * 0.25), max(2.0, vdur - 2.0)), video_path.with_name(f"{cand.slice_id}_ref_frame.png"))
    images: list[tuple[Path, str]] | None = []
    if sheet_path.exists():
        images.append((sheet_path, "MEDIA_RESOLUTION_MEDIUM"))
    if ref_frame_path is not None:
        images.append((ref_frame_path, "MEDIA_RESOLUTION_HIGH"))
    if not images:
        images = None

    word_snippet = " ".join(f"[{w.start:.1f}s]{w.word}" for w in transcript.words[:150])
    music_spans = [(round(ev.start, 1), round(ev.end, 1)) for ev in transcript.non_verbal_events if ev.kind == "music_spike"]
    music_span_str = f"[{music_spans[0][0]}s..{music_spans[-1][1]}s]" if music_spans else "none"
    brief_block = _match_brief_context(brief)
    bbox_note = (
        "Image 1 is a multi-frame contact sheet for temporal action analysis. "
        "Image 2 is a SINGLE FULL 16:9 FRAME: compute webcam_box_2d ([ymin, xmin, ymax, xmax] in 0..1000) "
        "STRICTLY on Image 2 (the single 16:9 frame), NEVER on the contact sheet grid! Set null if no PiP facecam overlay."
        if ref_frame_path is not None
        else "Set webcam_box_2d as [ymin, xmin, ymax, xmax] in 0..1000 ONLY around the main live speaking streamer's facecam (null if no facecam)."
    )
    prompt = (
        f"Audit slice_id='{cand.slice_id}' (duration={vdur:.1f}s, platform={cand.platform}, streamer={streamer_id}).\n"
        f"{brief_block}"
        f"AudioSet YAMNet telemetry ({transcript.dmca_detector}): music_prob_peak={transcript.music_prob_peak}, "
        f"dmca_flag={transcript.dmca_flag}, music_spike_window={music_span_str}, top_events={transcript.yamnet_top_events[:5]}.\n"
        f"Transcript with timestamps:\n{word_snippet}\n\n"
        "Classify scene_type strictly:\n"
        "- 'active_gameplay': live GTA VI gameplay with HUD/player control\n"
        "- 'trailer_or_video_reaction': watching a YouTube trailer or video\n"
        "- 'cutscene', 'menu_or_brb', 'just_chatting'\n"
        f"{bbox_note}\n"
        "Set matched_lore_entity if a GTA VI character, mission, Easter egg, or launch bug is clearly present.\n"
        "Cross-reference YAMNet anti-DMCA signal: reject audible licensed radio/club/trailer songs (dmca_clear=false). "
        "If music_spike_window is strictly outside a great 18s-45s gameplay window, suggest ONLY the music-free window and set dmca_clear=true. "
        "Also avoid suggesting windows containing stream meta-talk (gifted subs, donations, shoutouts).\n"
        "If active_gameplay and dmca_clear, suggest a tight 18s-45s clip window starting directly in action/reaction."
    )
    sys_inst = build_cached_system_instruction(
        "You are Pass-1 Multimodal Curator. Return SliceAuditResult JSON with exact scene classification, webcam bbox, lore match, and anti-DMCA check."
    )

    try:
        audit, _ = generate_structured(
            prompt,
            SliceAuditResult,
            system_instruction=sys_inst,
            images=images,
            media_resolution="MEDIA_RESOLUTION_HIGH",
        )
    except ValidationError:
        audit, _ = generate_structured(
            prompt + "\nCRITICAL: keep freeze_frame.callout_text <= 4 words and <= 32 chars (or null).",
            SliceAuditResult,
            system_instruction=sys_inst,
            images=images,
            media_resolution="MEDIA_RESOLUTION_HIGH",
        )

    audit.slice_id = cand.slice_id
    preset_px = _load_streamer_webcam_preset(streamer_id)
    audit.webcam_box_px = preset_px or _unscale_bbox_2d(audit.webcam_box_2d, vw, vh)
    min_start = 1.0 if (cand.platform == "kick" and cand.signal_source == "kick_clips_rank" and vdur >= 24.0) else 0.0
    audit.suggested_start_sec = max(min_start, min(max(min_start, vdur - 18.0), audit.suggested_start_sec))
    audit.suggested_end_sec = max(audit.suggested_start_sec + 16.0, min(vdur, audit.suggested_end_sec))
    return _apply_pass1_hard_gates(audit, transcript)


def _checked_by_code(plan: EditPlan, transcript: AnnotatedTranscript, max_dur: float) -> tuple[bool, list[str]]:
    """Hard deterministic code gate: duration, anti-DMCA, anti-meta-talk, speech density, and weak openers."""
    issues: list[str] = []
    dur = plan.end_trim - plan.start_trim
    if dur < 16.0 or dur > 58.0:
        issues.append(f"duration {dur:.1f}s outside [16, 58]")
    if plan.end_trim > max_dur + 0.15:
        issues.append(f"end_trim {plan.end_trim:.2f} > slice duration {max_dur:.2f}")
    if plan.scene_type != "active_gameplay":
        issues.append(f"scene_type={plan.scene_type} != active_gameplay")
    if not plan.dmca_clear:
        issues.append(f"dmca_clear=False ({plan.dmca_risk_reason})")

    all_in_window = [w for w in transcript.words if w.end > plan.start_trim and w.start < plan.end_trim]
    raw_text_low = " ".join(w.word for w in all_in_window).lower()
    for pat in _META_TALK_PATTERNS:
        if pat in raw_text_low:
            issues.append(f"meta-stream talk detected ({pat!r}) -> trim out or reject")
            break

    streamer_words = [w for w in all_in_window if not w.is_npc_or_other]
    if len(streamer_words) < 6:
        issues.append(f"too few streamer-spoken words ({len(streamer_words)} < 6) after LR-ASD filter")
    if streamer_words:
        first_norm = re.sub(r"[^\w']", "", streamer_words[0].word.lower())
        if first_norm in _WEAK_OPENERS:
            issues.append(f"weak opening word {streamer_words[0].word!r} -> trim forward")
        max_gap = max(
            [streamer_words[i + 1].start - streamer_words[i].end for i in range(len(streamer_words) - 1)],
            default=0.0,
        )
        if max_gap > 10.0:
            issues.append(f"dead-air silent gap {max_gap:.1f}s > 10.0s inside clip")

    if plan.freeze_frame is not None and plan.freeze_frame.timestamp_sec >= dur - 0.8:
        plan.freeze_frame.timestamp_sec = round(max(1.2, dur * 0.45), 2)
    return len(issues) == 0, issues


def _slice_transcript_text(transcript: AnnotatedTranscript, start_s: float, end_s: float) -> str:
    return " ".join(
        w.word for w in transcript.words if w.end > start_s and w.start < end_s and not w.is_npc_or_other
    ).strip()


def _run_blind_critic(
    plan: EditPlan,
    transcript: AnnotatedTranscript,
    code_ok: bool,
    sheet_path: Path | None,
) -> BlindCriticResult:
    clip_text = _slice_transcript_text(transcript, plan.start_trim, plan.end_trim)
    after_text = _slice_transcript_text(transcript, plan.end_trim, plan.end_trim + 5.0)
    win_music_spikes = [
        ev
        for ev in transcript.non_verbal_events
        if ev.kind == "music_spike" and ev.end > plan.start_trim and ev.start < plan.end_trim
    ]
    prompt = (
        f"Evaluate clip_id='{plan.clip_id}' (window={plan.start_trim:.1f}s..{plan.end_trim:.1f}s, "
        f"duration={plan.end_trim - plan.start_trim:.1f}s, layout={plan.layout_mode}, webcam_box_px={plan.webcam_box_px}).\n"
        f"CHECKED_BY_CODE_PASSED={code_ok}.\n"
        f"Trimmed-window YAMNet anti-DMCA check: dmca_clear={plan.dmca_clear}, "
        f"music_spikes_inside_clip={len(win_music_spikes)}, reason={plan.dmca_risk_reason!r}.\n"
        f"Hook headline: {plan.hook_headline!r}, matched_lore_entity={plan.matched_lore_entity!r}.\n"
        f"STREAMER-ONLY CLIP TRANSCRIPT:\n{clip_text or '(high-action visual moment)'}\n\n"
        f"AFTER THE CLIP (next 5s right after end_trim):\n{after_text or '(no speech in next 5s)'}\n\n"
        "Apply strict Blind Critic gates (the attached 8-frame strip shows the exact clip arc from start_trim to end_trim):\n"
        "- gate_alone: 'pass' only if an outsider understands in <3s with zero orphaned pronouns.\n"
        "- gate_ending: 'pass' only if payoff finishes inside clip and AFTER THE CLIP does NOT hold the climax.\n"
        "- Penalize heavily any stream meta-talk (gifted subs, donations, reading chat subs).\n"
        "- Score 5 axes (0..1) using calibrated anchors:\n"
        "  * 0.85-1.00 = standout viral gameplay/comedy/action payoff, clean audio/DMCA (0 music spikes = 0.92-1.0), clear facecam+gameplay framing.\n"
        "  * 0.75-0.84 = solid publishable Short with clear payoff and clean technicals.\n"
        "  * 0.50-0.74 = real structural flaw in hook, pacing, framing, or DMCA risk (do not publish).\n"
        "  * <0.50 = slop, meta-talk, cutscene, or broken clip.\n"
        "- Scores MUST be consistent with your qualitative feedback (do not write a glowing review and output <0.75).\n"
        "- Set approved=true ONLY if overall >= 0.75, checked_by_code_passed=true, gate_alone=='pass', gate_ending=='pass'."
    )
    sys_inst = build_cached_system_instruction(
        "You are Retention Blind Critic. Return BlindCriticResult JSON strictly enforcing the 5-axis rubric."
    )
    critic, _ = generate_structured(
        prompt,
        BlindCriticResult,
        system_instruction=sys_inst,
        images=[sheet_path] if (sheet_path and sheet_path.exists()) else None,
        media_resolution="MEDIA_RESOLUTION_MEDIUM",
    )
    critic.clip_id = plan.clip_id
    critic.checked_by_code_passed = code_ok
    if not code_ok or critic.gate_alone != "pass" or critic.gate_ending != "pass" or critic.overall < 0.75:
        critic.approved = False
    return critic


def curate_pass2_slice(
    cand: CandidateSlice,
    transcript: AnnotatedTranscript,
    audit: SliceAuditResult,
    run_dir: Path,
    voice_mode: str = "none",
    brief: str | None = None,
    max_retries: int = 1,
) -> tuple[EditPlan | None, BlindCriticResult]:
    """Pass 2: Run LR-ASD speaker separation, 1fps zoom strip TrimRefiner, CHECKED BY CODE & Blind Critic."""
    video_path = Path(cand.slice_video_path)
    _, _, vdur = _probe_video_geometry(video_path)

    # Run LR-ASD ONNX strictly inside streamer webcam crop before TrimRefiner & captions
    if audit.webcam_box_px:
        asd_info = score_streamer_speech_asd(video_path, audit.webcam_box_px, transcript.words)
        print(f"[LR_ASD] {cand.slice_id}: {asd_info}")
        (run_dir / f"{cand.slice_id}_transcript.json").write_text(
            transcript.model_dump_json(indent=2), encoding="utf-8"
        )
    else:
        print(f"[LR_ASD] {cand.slice_id}: skipped (no webcam_box_px -> full_gameplay_9_16 fallback)")

    zoom_png = run_dir / f"{cand.slice_id}_zoom_1fps.png"
    zoom_span = max(16.0, min(vdur - audit.suggested_start_sec, (audit.suggested_end_sec - audit.suggested_start_sec) + 5.0))
    _extract_zoom_strip_1fps(video_path, audit.suggested_start_sec, zoom_span, zoom_png)

    word_lines = " ".join(
        f"[{w.start:.2f}s{' NPC' if w.is_npc_or_other else ''}]{w.word}" for w in transcript.words
    )
    sys_refine = build_cached_system_instruction(
        "You are Pass-2 Director & TrimRefiner. Pick exact 18s-45s trims opening directly on action/reaction, "
        "ZERO stream meta-talk (gifted subs/donations), a punchy <=10-word hook_headline, matched_lore_entity, "
        "and 1 <=4-word FreezeFrameSpec at the comedic/action peak."
    )
    brief_block = _match_brief_context(brief)
    min_start = 1.0 if (cand.platform == "kick" and cand.signal_source == "kick_clips_rank" and vdur >= 24.0) else 0.0

    feedback_note = ""
    last_critic: BlindCriticResult | None = None
    best_plan: EditPlan | None = None

    for _attempt in range(max_retries + 1):
        prompt = (
            f"Refine slice_id='{cand.slice_id}' into clip_id='clip_{cand.slice_id[-2:]}'.\n"
            f"{brief_block}"
            f"Pass-1 window: {audit.suggested_start_sec:.2f}s -> {audit.suggested_end_sec:.2f}s (valid range {min_start:.1f}s..{vdur:.2f}s).\n"
            f"Scene type: {audit.scene_type}, YAMNet music_prob_peak={transcript.music_prob_peak}.\n"
            f"Timed words (words marked NPC are game characters and will be hidden from subs):\n{word_lines}\n"
            f"{feedback_note}\n"
            "Rules:\n"
            "- Output 18s <= (end_trim - start_trim) <= 45s (<= 55s max).\n"
            "- Exclude ANY segment with sub thank-yous ('gifted', 'sub', 'donation', 'shoutout').\n"
            "- Never start on a weak filler ('um', 'uh', 'so', 'and').\n"
            "- Include 1 peak freeze_frame (`callout_text` 1..4 words <= 24 chars, timestamp_sec relative to start_trim)."
        )
        try:
            prop, _ = generate_structured(
                prompt,
                _Pass2TrimProposal,
                system_instruction=sys_refine,
                images=[zoom_png],
                media_resolution="MEDIA_RESOLUTION_HIGH",
            )
        except ValidationError as exc:
            prop, _ = generate_structured(
                prompt + f"\nFIX VALIDATION ERROR: {str(exc)[:180]}",
                _Pass2TrimProposal,
                system_instruction=sys_refine,
                images=[zoom_png],
                media_resolution="MEDIA_RESOLUTION_HIGH",
            )

        s_trim = max(min_start, min(max(min_start, vdur - 18.0), round(prop.start_trim, 2)))
        e_trim = min(vdur, max(s_trim + 17.0, round(prop.end_trim, 2)))
        if e_trim - s_trim > 55.0:
            e_trim = round(s_trim + 45.0, 2)

        clip_d = e_trim - s_trim
        ff = prop.freeze_frame
        if ff is None:
            hl_words = [w for w in prop.hook_headline.strip().split() if w][:3]
            fb_text = " ".join(hl_words)[:28].strip() or "WAIT FOR IT"
            ff = FreezeFrameSpec(
                timestamp_sec=round(max(2.0, min(clip_d - 2.0, clip_d * 0.45)), 2),
                box_2d=(300, 250, 750, 750),
                callout_text=fb_text,
                duration_sec=0.8,
            )
        elif ff.timestamp_sec >= clip_d - 1.0 or ff.timestamp_sec < 1.0:
            ff = FreezeFrameSpec(
                timestamp_sec=round(max(1.5, min(clip_d - 1.5, clip_d * 0.45)), 2),
                box_2d=ff.box_2d,
                callout_text=ff.callout_text,
                duration_sec=min(1.0, max(0.6, ff.duration_sec)),
            )

        plan = EditPlan(
            clip_id=f"clip_{cand.slice_id[-2:]}",
            slice_id=cand.slice_id,
            source_url=cand.source_url,
            start_trim=s_trim,
            end_trim=e_trim,
            scene_type=audit.scene_type,
            visual_hook_score=audit.visual_hook_score,
            hook_headline=prop.hook_headline,
            voice_mode="none" if voice_mode not in {"human_batch", "tts"} else voice_mode,  # type: ignore[arg-type]
            clip_lane="trend_multi_pov" if prop.matched_trend_id else "discovery",
            matched_trend_id=prop.matched_trend_id,
            matched_lore_entity=prop.matched_lore_entity or audit.matched_lore_entity,
            layout_mode="split_webcam_top" if audit.webcam_box_px else "full_gameplay_9_16",
            webcam_box_2d=audit.webcam_box_2d,
            webcam_box_px=audit.webcam_box_px,
            freeze_frame=ff,
            dmca_clear=audit.dmca_clear and prop.dmca_clear,
            dmca_risk_reason=prop.dmca_risk_reason or audit.dmca_risk_reason,
            self_contained_score=prop.self_contained_score,
            rationale=prop.rationale,
        )

        code_ok, code_issues = _checked_by_code(plan, transcript, vdur)
        critic_strip = run_dir / f"{plan.clip_id}_critic_strip.png"
        _extract_zoom_strip_1fps(video_path, plan.start_trim, max(12.0, plan.end_trim - plan.start_trim), critic_strip)
        critic = _run_blind_critic(plan, transcript, code_ok, critic_strip)
        best_plan = plan
        last_critic = critic
        if critic.approved:
            return plan, critic
        feedback_note = f"PREVIOUS ATTEMPT FAILED (fix strictly): code_issues={code_issues}; critic_feedback={critic.feedback}"

    assert last_critic is not None
    return (best_plan if last_critic.approved else None), last_critic


def generate_clip_packaging(plan: EditPlan, transcript: AnnotatedTranscript, streamer_id: str) -> ClipPackaging:
    """Generate 5-network ClipPackaging (YT Shorts, TikTok, IG Reels, FB Reels, X) for an approved master clip."""
    clip_text = _slice_transcript_text(transcript, plan.start_trim, plan.end_trim)
    prompt = (
        f"Generate algorithmic 5-network packaging for approved GTA VI clip_id='{plan.clip_id}' (streamer={streamer_id}).\n"
        f"Hook headline: {plan.hook_headline!r}\n"
        f"Matched lore entity: {plan.matched_lore_entity!r}\n"
        f"Duration: {plan.end_trim - plan.start_trim:.1f}s, voice_mode={plan.voice_mode}, dmca_clear={plan.dmca_clear}.\n"
        f"Streamer transcript: {clip_text}\n\n"
        "Constraints:\n"
        "- shorts_title: <= 60 characters, Tier-1 English, punchy, no AI-slop cliches.\n"
        "- shorts_hashtags: 3 to 5 clean hashtags (include #GTA6 #Shorts).\n"
        "- suggested_on_screen_hook: <= 6 words.\n"
        "- tiktok_caption: 1 conversational line prompting benign debate.\n"
        "- reels_caption: short copy with save/DM share CTA.\n"
        "- facebook_reels_caption: nostalgic or community-debate 1-2 line caption inviting shares/comments.\n"
        "- x_post_text: punchy native 1-liner <= 220 chars, zero external links, max 1-2 hashtags (#GTA6).\n"
        "- bounty_safe: true only if dmca_clear and voice_mode=='none'."
    )
    sys_inst = build_cached_system_instruction(
        "You are Marketing Strategist. Return ClipPackaging JSON following Marketing Playbook and Golden References."
    )
    try:
        pkg, _ = generate_structured(prompt, ClipPackaging, system_instruction=sys_inst)
    except ValidationError as exc:
        pkg, _ = generate_structured(
            prompt + f"\nSTRICT LENGTH FIX: {str(exc)[:180]}",
            ClipPackaging,
            system_instruction=sys_inst,
        )
    pkg.clip_id = plan.clip_id
    pkg.bounty_safe = bool(plan.dmca_clear and plan.voice_mode == "none")
    return pkg


def curate_run(run_dir: Path, brief: str | None = None) -> RunState:
    """Run Pass-1 + LR-ASD + Pass-2 curation over all transcribed slices in runs/<run_id>/."""
    state_path = run_dir / "state.json"
    state = RunState.model_validate_json(state_path.read_text(encoding="utf-8"))

    voice_mode = os.getenv("VOICE_MODE", "none").strip() or "none"
    if CHANNEL_PROFILE_PATH.exists():
        cfg = yaml.safe_load(CHANNEL_PROFILE_PATH.read_text(encoding="utf-8")) or {}
        voice_mode = str(cfg.get("voice", {}).get("mode", voice_mode))

    tr_map = {t.slice_id: t for t in state.transcripts}
    audits: list[SliceAuditResult] = []
    plans: list[EditPlan] = []
    critics: list[BlindCriticResult] = []

    for cand in state.candidates:
        tr = tr_map.get(cand.slice_id)
        if not tr:
            continue
        audit = audit_slice_pass1(cand, tr, state.streamer_id, brief=brief)
        audits.append(audit)
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
            scene_type=audit.scene_type,
            approved_p2=audit.approved_for_pass2,
        )
        upsert_transcript_fts(state.run_id, cand.slice_id, tr.full_text, audit.matched_lore_entity or "")
        print(
            f"[CURATOR_P1] {cand.slice_id}: scene={audit.scene_type} dmca_clear={audit.dmca_clear} "
            f"approved_p2={audit.approved_for_pass2} cam_px={audit.webcam_box_px} lore={audit.matched_lore_entity}"
        )

    approved_audits = [a for a in audits if a.approved_for_pass2]
    approved_audits.sort(key=lambda a: (a.visual_hook_score, a.self_contained_score), reverse=True)
    cand_map = {c.slice_id: c for c in state.candidates}

    for audit in approved_audits:
        cand = cand_map[audit.slice_id]
        tr = tr_map[audit.slice_id]
        plan, critic = curate_pass2_slice(cand, tr, audit, run_dir, voice_mode=voice_mode, brief=brief, max_retries=1)
        critics.append(critic)
        print(
            f"[CURATOR_P2] {audit.slice_id}: overall={critic.overall:.2f} alone={critic.gate_alone} "
            f"ending={critic.gate_ending} approved={critic.approved}"
        )
        if plan is not None and critic.approved:
            plans.append(plan)
            break

    state.slice_audits = audits
    state.edit_plans = plans
    state.critic_results = critics
    state.stage = "curated"
    state.updated_at = datetime.now(UTC).isoformat()
    state_path.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    return state
