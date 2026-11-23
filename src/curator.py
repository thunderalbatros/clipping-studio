"""Multimodal 2-pass Curator, 1fps TrimRefiner, CHECKED BY CODE, and Blind Critic (Hito 1.3)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

import yaml
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.llm_router import build_cached_system_instruction, generate_structured
from src.miner import CHANNEL_PROFILE_PATH, PROJECT_ROOT
from src.schemas import (
    AnnotatedTranscript,
    BlindCriticResult,
    CandidateSlice,
    EditPlan,
    FreezeFrameSpec,
    RunState,
    SliceAuditResult,
)

STREAMERS_PATH = PROJECT_ROOT / "config" / "streamers.yaml"
_WEAK_OPENERS = {"um", "uh", "uhh", "umm", "so", "and", "but", "or", "because", "then"}


class _Pass2TrimProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clip_id: str
    start_trim: float = Field(ge=0.0)
    end_trim: float = Field(gt=0.0)
    hook_headline: str
    freeze_frame: FreezeFrameSpec | None = None
    matched_trend_id: str | None = None
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
    """Convert normalized Gemini [ymin, xmin, ymax, xmax] 0..1000 into even pixel [x, y, w, h]."""
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


def _extract_zoom_strip_1fps(video_path: Path, start_sec: float, duration_sec: float, out_png: Path) -> Path:
    """Extract 8 frames at 1fps from candidate action start/peak into a 4x2 PNG strip."""
    step = max(1.0, min(2.0, duration_sec / 8.0))
    timestamps = [round(start_sec + i * step, 2) for i in range(8)]
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
    """Enforce deterministic non-gameplay rejection and cross-referenced anti-DMCA rules."""
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
            or f"Rejected by anti-DMCA gate: music_prob_peak={transcript.music_prob_peak} (>={0.45})"
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


def audit_slice_pass1(cand: CandidateSlice, transcript: AnnotatedTranscript, streamer_id: str) -> SliceAuditResult:
    """Pass 1: Send contact_sheet.png + transcript to Gemini 3.8 Flash Vision for scene/DMCA/webcam audit."""
    video_path = Path(cand.slice_video_path)
    vw, vh, vdur = _probe_video_geometry(video_path)
    sheet_path = Path(transcript.contact_sheet_path) if transcript.contact_sheet_path else video_path.with_name(f"{cand.slice_id}_contact_sheet.png")
    images = [sheet_path] if sheet_path.exists() else None

    word_snippet = " ".join(f"[{w.start:.1f}s]{w.word}" for w in transcript.words[:140])
    prompt = (
        f"Audit slice_id='{cand.slice_id}' (duration={vdur:.1f}s, platform={cand.platform}).\n"
        f"Acoustic telemetry: music_prob_peak={transcript.music_prob_peak}, dmca_flag={transcript.dmca_flag}.\n"
        f"Non-verbal events: {[(e.kind, e.start, e.end, e.note) for e in transcript.non_verbal_events]}.\n"
        f"Transcript with timestamps:\n{word_snippet}\n\n"
        "Classify scene_type strictly:\n"
        "- 'active_gameplay': streamer playing live GTA VI world/missions with HUD/controller action\n"
        "- 'trailer_or_video_reaction': streamer watching a YouTube trailer, movie, or another video\n"
        "- 'cutscene', 'menu_or_brb', 'just_chatting'\n"
        "Set webcam_box_2d as [ymin, xmin, ymax, xmax] in 0..1000 coordinates of the streamer facecam if visible.\n"
        "Cross-reference anti-DMCA signal carefully:\n"
        "- dmca_flag=True (>=0.45) is an acoustic sustain alert that triggers BOTH on licensed radio/club/trailer music "
        "AND on steady vehicle/airplane engine noise or nature ambience during speech pauses.\n"
        "- If trailer_or_video_reaction, radio wheel/club/background commercial music visible, or song lyrics/singing in transcript -> set dmca_clear=false.\n"
        "- If pure active_gameplay where the sustain clearly comes from airplane/vehicle engine or world SFX with no licensed song -> set dmca_clear=true (keep suggested clip <= 55s).\n"
        "Reject non-gameplay (approved_for_pass2=false) or real licensed music.\n"
        "If active_gameplay and dmca_clear, suggest a tight 22s-48s clip window (suggested_start_sec, suggested_end_sec) "
        "starting directly in action/reaction, plus a <=4-word callout if appropriate."
    )
    sys_inst = build_cached_system_instruction(
        "You are Pass-1 Multimodal Curator. Return SliceAuditResult JSON with exact scene classification, webcam bbox, and anti-DMCA check."
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
        # Retry once if LLM exceeded freeze_frame.callout_text word limit in constrained decoding
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
    audit.suggested_start_sec = max(0.0, min(vdur - 20.0, audit.suggested_start_sec))
    audit.suggested_end_sec = max(audit.suggested_start_sec + 18.0, min(vdur, audit.suggested_end_sec))
    return _apply_pass1_hard_gates(audit, transcript)


def _checked_by_code(plan: EditPlan, transcript: AnnotatedTranscript, max_dur: float) -> tuple[bool, list[str]]:
    issues: list[str] = []
    dur = plan.end_trim - plan.start_trim
    if dur < 18.0 or dur > 58.0:
        issues.append(f"duration {dur:.1f}s outside [18, 58]")
    if plan.end_trim > max_dur + 0.1:
        issues.append(f"end_trim {plan.end_trim:.2f} > slice duration {max_dur:.2f}")
    if plan.scene_type != "active_gameplay":
        issues.append(f"scene_type={plan.scene_type} != active_gameplay")
    if not plan.dmca_clear:
        issues.append(f"dmca_clear=False ({plan.dmca_risk_reason})")

    in_window = [w for w in transcript.words if w.end > plan.start_trim and w.start < plan.end_trim]
    if len(in_window) < 12:
        issues.append(f"too few spoken words ({len(in_window)} < 12) for active commentary")
    if in_window:
        first_norm = re.sub(r"[^\w']", "", in_window[0].word.lower())
        if first_norm in _WEAK_OPENERS:
            issues.append(f"weak opening word {in_window[0].word!r} -> trim forward")
        max_gap = max(
            [in_window[i + 1].start - in_window[i].end for i in range(len(in_window) - 1)],
            default=0.0,
        )
        if max_gap > 8.0:
            issues.append(f"dead-air silent gap {max_gap:.1f}s > 8.0s inside clip")
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
    prompt = (
        f"Evaluate clip_id='{plan.clip_id}' (duration={plan.end_trim - plan.start_trim:.1f}s).\n"
        f"CHECKED_BY_CODE_PASSED={code_ok}.\n"
        f"Hook headline: {plan.hook_headline!r}\n"
        f"CLIP TRANSCRIPT:\n{clip_text or '(mostly non-verbal/gameplay)'}\n\n"
        f"AFTER THE CLIP (next 5s right after end_trim):\n{after_text or '(no speech in next 5s)'}\n\n"
        "Apply strict Blind Critic gates:\n"
        "- gate_alone: 'pass' only if an outsider understands in <3s with zero orphaned pronouns.\n"
        "- gate_ending: 'pass' only if the payoff/punchline finishes inside the clip and AFTER THE CLIP does NOT hold the real climax.\n"
        "- Score 5 axes (0..1): hook_3s, pacing, visual_framing, audio_dmca_safety, packaging_fit, overall.\n"
        "- Set approved=true ONLY if overall >= 0.75, checked_by_code_passed=true, gate_alone=='pass', and gate_ending=='pass'."
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
    max_retries: int = 1,
) -> tuple[EditPlan | None, BlindCriticResult]:
    """Pass 2: Extract 1fps zoom strip, refine trims with TrimRefiner, run CHECKED BY CODE & Blind Critic."""
    video_path = Path(cand.slice_video_path)
    _, _, vdur = _probe_video_geometry(video_path)
    zoom_png = run_dir / f"{cand.slice_id}_zoom_1fps.png"
    _extract_zoom_strip_1fps(video_path, audit.suggested_start_sec, 12.0, zoom_png)

    word_lines = " ".join(f"[{w.start:.2f}s]{w.word}" for w in transcript.words)
    sys_refine = build_cached_system_instruction(
        "You are Pass-2 Director & TrimRefiner. Pick exact 22s-45s trims opening directly on action/reaction, "
        "a punchy <=10-word hook_headline, and at most 1 <=4-word FreezeFrameSpec (relative timestamp_sec inside clip)."
    )

    feedback_note = ""
    last_critic: BlindCriticResult | None = None
    best_plan: EditPlan | None = None

    for attempt in range(max_retries + 1):
        prompt = (
            f"Refine slice_id='{cand.slice_id}' into clip_id='clip_{cand.slice_id[-2:]}'.\n"
            f"Pass-1 suggested window: {audit.suggested_start_sec:.2f}s -> {audit.suggested_end_sec:.2f}s (max={vdur:.2f}s).\n"
            f"Scene type: {audit.scene_type}, music_prob_peak={transcript.music_prob_peak}.\n"
            f"Timed words:\n{word_lines}\n"
            f"{feedback_note}\n"
            "Rules:\n"
            "- Output 22s <= (end_trim - start_trim) <= 45s (<= 55s max).\n"
            "- Never start on a weak filler ('um', 'uh', 'so', 'and').\n"
            "- Avoid dead-air silent gaps > 6s; keep active streamer reaction throughout.\n"
            "- Always include 1 peak freeze_frame (`FreezeFrameSpec` with `callout_text` of 1 to 4 words max <= 24 chars) at the funniest or highest-chaos moment.\n"
            "- freeze_frame.timestamp_sec MUST be relative to start_trim (between 2.0 and clip_duration - 2.0)."
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

        s_trim = max(0.0, min(vdur - 20.0, round(prop.start_trim, 2)))
        e_trim = min(vdur, max(s_trim + 20.0, round(prop.end_trim, 2)))
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
            webcam_box_2d=audit.webcam_box_2d,
            webcam_box_px=audit.webcam_box_px,
            freeze_frame=ff,
            dmca_clear=audit.dmca_clear and prop.dmca_clear,
            dmca_risk_reason=prop.dmca_risk_reason or audit.dmca_risk_reason,
            self_contained_score=prop.self_contained_score,
            rationale=prop.rationale,
        )

        code_ok, code_issues = _checked_by_code(plan, transcript, vdur)
        critic = _run_blind_critic(plan, transcript, code_ok, zoom_png)
        best_plan = plan
        last_critic = critic
        if critic.approved:
            return plan, critic
        feedback_note = f"PREVIOUS ATTEMPT FAILED (fix strictly): code_issues={code_issues}; critic_feedback={critic.feedback}"

    assert last_critic is not None
    return (best_plan if last_critic.approved else None), last_critic


def curate_run(run_dir: Path) -> RunState:
    """Run Pass-1 and Pass-2 curation over all transcribed slices in runs/<run_id>/."""
    from datetime import UTC, datetime

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
        audit = audit_slice_pass1(cand, tr, state.streamer_id)
        audits.append(audit)
        print(
            f"[CURATOR_P1] {cand.slice_id}: scene={audit.scene_type} dmca_clear={audit.dmca_clear} "
            f"approved_p2={audit.approved_for_pass2} cam_px={audit.webcam_box_px}"
        )

    approved_audits = [a for a in audits if a.approved_for_pass2]
    approved_audits.sort(key=lambda a: (a.visual_hook_score, a.self_contained_score), reverse=True)
    cand_map = {c.slice_id: c for c in state.candidates}

    for audit in approved_audits:
        cand = cand_map[audit.slice_id]
        tr = tr_map[audit.slice_id]
        plan, critic = curate_pass2_slice(cand, tr, audit, run_dir, voice_mode=voice_mode, max_retries=1)
        critics.append(critic)
        print(
            f"[CURATOR_P2] {audit.slice_id}: overall={critic.overall:.2f} alone={critic.gate_alone} "
            f"ending={critic.gate_ending} approved={critic.approved}"
        )
        if plan is not None and critic.approved:
            plans.append(plan)
            break  # Deliver 1 top approved master clip per run in v1

    state.slice_audits = audits
    state.edit_plans = plans
    state.critic_results = critics
    state.stage = "curated"
    state.updated_at = datetime.now(UTC).isoformat()
    state_path.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    return state
