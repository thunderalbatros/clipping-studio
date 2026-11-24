"""Autonomous Post-Render Visual QA Gate powered by `pydantic-evals` + `pydantic-ai` (`gemini-3.8-flash`, v3.6-A).

Combines deterministic binary runtime evaluators (`media_check`: `1080x1920`, `60 fps` CFR, `-14.0 LUFS`,
`UNIVERSAL_SAFE_BOX_9_16 = [48, 420, 888, 1248]`, `cover != overlay` anti-duplication) with a strict VLM judge
over the 4 `qa_frame_*_safezones.png` + `cover_9_16.jpg` grounded in RAG `design_ctr` principles.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import EvaluationReason, Evaluator, EvaluatorContext, EvaluatorOutput

from evals.media_check import evaluate_video
from src.llm_router import (
    build_cached_system_instruction,
    generate_structured,
    wrap_untrusted_source_data,
)
from src.rag_engine import format_rag_context, query_rag
from src.schemas import UNIVERSAL_SAFE_BOX_9_16

RecommendedFix = Literal["none", "shift_cover_frame", "alternate_cover_hook", "shift_overlay_y"]


class VisualQAVlmVerdict(BaseModel):
    """Structured multimodal QA judgment over 4 safe-zone video frames + 9:16 editorial cover."""

    model_config = ConfigDict(extra="forbid")

    no_hud_or_right_button_collision: bool
    no_cover_overlay_duplication: bool
    no_halos_or_floating_artifacts: bool
    face_unobstructed_on_cover: bool
    mobile_legibility_score: float = Field(ge=0.0, le=1.0)
    anti_ai_slop_score: float = Field(ge=0.0, le=1.0)
    overall_visual_score: float = Field(ge=0.0, le=1.0)
    recommended_parametric_fix: RecommendedFix = "none"
    issues: list[str] = Field(default_factory=list)
    rationale: str = Field(max_length=400)


@dataclass
class VisualQAInputs:
    clip_id: str
    video_path: Path
    cover_path: Path | None = None
    qa_dir: Path | None = None
    expected_fps: float | None = 60.0
    shorts_title: str = ""
    cover_headline: str = ""
    overlay_callout_text: str = ""
    run_vlm: bool = True


VlmJudgeFn = Callable[[VisualQAInputs, list[Path], str], tuple[VisualQAVlmVerdict, str]]


def _norm_words(text: str) -> str:
    clean = re.sub(r"['\u2019`]", "", (text or "").lower())
    return " ".join(re.findall(r"[a-z0-9]+", clean))


def is_cover_overlay_duplicated(cover_headline: str, overlay_callout_text: str) -> bool:
    """Return True if the 9:16 cover hook literally repeats the interior motion-overlay text."""
    c_norm = _norm_words(cover_headline)
    o_norm = _norm_words(overlay_callout_text)
    if not c_norm or not o_norm:
        return False
    if c_norm == o_norm:
        return True
    c_toks = {t for t in c_norm.split() if len(t) >= 3}
    o_toks = {t for t in o_norm.split() if len(t) >= 3}
    return bool(c_toks and o_toks and (c_toks == o_toks or c_toks.issubset(o_toks) or o_toks.issubset(c_toks)))


def _default_vlm_judge(
    inputs: VisualQAInputs,
    images: list[Path],
    rag_principles: str,
) -> tuple[VisualQAVlmVerdict, str]:
    sys_inst = build_cached_system_instruction(
        "You are Retention Critic & Senior Art Director executing strict post-render Visual QA. "
        "Inspect the 4 rendered 9:16 video QA frames (with Amber TikTok/Reels & Red Universal Safe Box [48..888, 420..1248] guides) "
        "plus the final 9:16 cover image (if attached). Return VisualQAVlmVerdict JSON."
    )
    meta_block = wrap_untrusted_source_data(
        "clip_packaging_metadata",
        (
            f"clip_id={inputs.clip_id}\n"
            f"shorts_title={inputs.shorts_title}\n"
            f"cover_headline={inputs.cover_headline}\n"
            f"overlay_callout_text={inputs.overlay_callout_text}"
        ),
    )
    rag_block = (
        f"RAG DESIGN & ANTI-SLOP PRINCIPLES:\n{wrap_untrusted_source_data('rag_design_ctr', rag_principles)}\n\n"
        if rag_principles
        else ""
    )
    prompt = (
        f"Audit rendered vertical clip '{inputs.clip_id}' across the attached {len(images)} images "
        "(first 4 = rendered video frames at 10%/35%/65%/85% with Safe-Zone overlays; 5th if present = 9:16 cover_9_16.jpg).\n\n"
        f"{rag_block}"
        f"{meta_block}\n\n"
        "Checklist (be honest and strict, never rubber-stamp broken visuals):\n"
        "1. `no_hud_or_right_button_collision`: All burned captions and editorial motion overlays stay cleanly readable "
        "inside the Red Safe Box [x: 48..888, y: 420..1248] without colliding into right-side UI buttons (x > 888) or bottom chrome.\n"
        "2. `no_cover_overlay_duplication`: `cover_headline` must NOT repeat `overlay_callout_text` verbatim.\n"
        "3. `no_halos_or_floating_artifacts`: No broken seam borders, floating cutout islands, black dead bars, or corrupted renders.\n"
        "4. `face_unobstructed_on_cover`: On the 9:16 cover (if present), streamer's face/eyes/mouth are NOT covered by a hand/mic occlusion or headline text.\n"
        "5. Score `mobile_legibility_score`, `anti_ai_slop_score`, and `overall_visual_score` in [0.0, 1.0] (pass floor >= 0.72).\n"
        "6. If any check fails, choose `recommended_parametric_fix` in ('shift_cover_frame', 'alternate_cover_hook', 'shift_overlay_y')."
    )
    return generate_structured(
        prompt,
        VisualQAVlmVerdict,
        system_instruction=sys_inst,
        images=list(images),
        media_resolution="MEDIA_RESOLUTION_MEDIUM",
    )


@dataclass
class DeterministicMediaEvaluator(Evaluator[VisualQAInputs, dict[str, Any], dict[str, Any]]):
    """Deterministic `pydantic-evals` evaluator for CFR FPS, `-14 LUFS`, opening 1.5s, and Safe-Zone frames."""

    def evaluate(self, ctx: EvaluatorContext[VisualQAInputs, dict[str, Any], dict[str, Any]]) -> EvaluatorOutput:
        mc = ctx.output.get("media_check") or {}
        checks = mc.get("checks") or {}
        ff = checks.get("ffprobe_spec") or {}
        op = checks.get("opening_integrity_1_5s") or {}
        lu = checks.get("loudness_ebur128") or {}
        qf = checks.get("qa_frames_png") or {}

        cfr_ok = bool(ff.get("is_cfr")) and bool(ff.get("passed"))
        lufs_val = float(lu.get("integrated_lufs") or -99.0)
        tp_val = float(lu.get("true_peak_dbtp") or 0.0)
        lufs_ok = bool(lu.get("passed"))
        open_ok = bool(op.get("passed"))
        frames_ok = bool(qf.get("passed"))

        return {
            "cfr_and_geometry_ok": EvaluationReason(
                value=cfr_ok,
                reason=f"{ff.get('width')}x{ff.get('height')} avg_fps={ff.get('avg_fps')} r_fps={ff.get('r_fps')} cfr={ff.get('is_cfr')}",
            ),
            "loudness_ebu_ok": EvaluationReason(
                value=lufs_ok,
                reason=f"integrated={lufs_val} LUFS, true_peak={tp_val} dBTP",
            ),
            "opening_1_5s_clean": EvaluationReason(
                value=open_ok,
                reason=f"black={op.get('black_starts')}, freeze={op.get('freeze_starts')}",
            ),
            "safezone_frames_extracted": EvaluationReason(
                value=frames_ok,
                reason=f"safe_box={list(UNIVERSAL_SAFE_BOX_9_16)} count={len(qf.get('safezone_files') or [])}",
            ),
        }


@dataclass
class PackagingAntiSlopEvaluator(Evaluator[VisualQAInputs, dict[str, Any], dict[str, Any]]):
    """Deterministic `pydantic-evals` evaluator for `UNIVERSAL_SAFE_BOX_9_16`, cover geometry, and `cover != overlay`."""

    def evaluate(self, ctx: EvaluatorContext[VisualQAInputs, dict[str, Any], dict[str, Any]]) -> EvaluatorOutput:
        inp = ctx.inputs
        dup = is_cover_overlay_duplicated(inp.cover_headline, inp.overlay_callout_text)
        safe_box_ssot = UNIVERSAL_SAFE_BOX_9_16 == (48, 420, 888, 1248)

        cover_dim_ok = True
        cover_reason = "no_cover_provided"
        if inp.cover_path is not None:
            if inp.cover_path.is_file():
                with Image.open(inp.cover_path) as im:
                    sz = im.size
                cover_dim_ok = sz == (1080, 1920)
                cover_reason = f"cover_size={sz}"
            else:
                cover_dim_ok = False
                cover_reason = f"missing_cover:{inp.cover_path}"

        return {
            "universal_safe_box_420_ok": EvaluationReason(
                value=safe_box_ssot,
                reason=f"UNIVERSAL_SAFE_BOX_9_16={list(UNIVERSAL_SAFE_BOX_9_16)}",
            ),
            "cover_not_duplicating_overlay": EvaluationReason(
                value=not dup,
                reason=f"cover={inp.cover_headline!r} vs overlay={inp.overlay_callout_text!r}",
            ),
            "cover_dimensions_ok": EvaluationReason(
                value=cover_dim_ok,
                reason=cover_reason,
            ),
        }


@dataclass
class VlmVisualCriticEvaluator(Evaluator[VisualQAInputs, dict[str, Any], dict[str, Any]]):
    """`pydantic-evals` VLM evaluator inspecting rendered Safe-Zone frames + 9:16 cover."""

    def evaluate(self, ctx: EvaluatorContext[VisualQAInputs, dict[str, Any], dict[str, Any]]) -> EvaluatorOutput:
        vlm_dict = ctx.output.get("vlm_verdict")
        if not vlm_dict:
            return {
                "vlm_visual_gate_ok": EvaluationReason(value=True, reason="vlm_skipped"),
            }
        verdict = VisualQAVlmVerdict.model_validate(vlm_dict)
        vlm_pass = (
            verdict.no_hud_or_right_button_collision
            and verdict.no_cover_overlay_duplication
            and verdict.no_halos_or_floating_artifacts
            and verdict.face_unobstructed_on_cover
            and verdict.mobile_legibility_score >= 0.70
            and verdict.anti_ai_slop_score >= 0.70
            and verdict.overall_visual_score >= 0.72
        )
        return {
            "vlm_visual_gate_ok": EvaluationReason(value=vlm_pass, reason=verdict.rationale),
            "vlm_hud_buttons_clear": verdict.no_hud_or_right_button_collision,
            "vlm_face_unobstructed": verdict.face_unobstructed_on_cover,
            "vlm_no_halos_artifacts": verdict.no_halos_or_floating_artifacts,
            "vlm_overall_score": round(float(verdict.overall_visual_score), 3),
            "vlm_legibility_score": round(float(verdict.mobile_legibility_score), 3),
            "vlm_anti_slop_score": round(float(verdict.anti_ai_slop_score), 3),
        }


def evaluate_visual_qa(
    inputs: VisualQAInputs,
    *,
    vlm_judge_fn: VlmJudgeFn | None = None,
    out_report_json: Path | None = None,
) -> dict[str, Any]:
    """Execute full `pydantic-evals` post-render Visual QA suite and return structured gate report."""
    judge = vlm_judge_fn or _default_vlm_judge

    def _task(case_in: VisualQAInputs) -> dict[str, Any]:
        qa_dir = case_in.qa_dir or (case_in.video_path.parent / f"{case_in.clip_id}_qa_frames")
        mc = evaluate_video(case_in.video_path, qa_dir=qa_dir, expected_fps=case_in.expected_fps)
        sz_paths = [
            Path(p)
            for p in ((mc.get("checks") or {}).get("qa_frames_png") or {}).get("safezone_files", [])
            if Path(p).is_file()
        ]
        attach: list[Path] = list(sz_paths[:4])
        if case_in.cover_path is not None and case_in.cover_path.is_file():
            attach.append(case_in.cover_path)

        rag_ctx = ""
        try:
            hits = query_rag(
                "9:16 safe box right buttons curiosity gap cover squint APCA halos",
                domain="design_ctr",
                top_k=3,
                use_vectors=True,
            )
            rag_ctx = format_rag_context(hits, max_tokens=320)
        except Exception:
            rag_ctx = ""

        vlm_payload: dict[str, Any] | None = None
        vlm_provider = "skipped"
        if case_in.run_vlm and attach:
            vlm_obj, vlm_provider = judge(case_in, attach, rag_ctx)
            # Deterministic cross-check: if code detected cover==overlay duplication, force flag False
            if is_cover_overlay_duplicated(case_in.cover_headline, case_in.overlay_callout_text):
                vlm_obj.no_cover_overlay_duplication = False
                if vlm_obj.recommended_parametric_fix == "none":
                    vlm_obj.recommended_parametric_fix = "alternate_cover_hook"
            vlm_payload = vlm_obj.model_dump()

        return {
            "media_check": mc,
            "vlm_verdict": vlm_payload,
            "vlm_provider": vlm_provider,
            "inspected_images": [str(p) for p in attach],
        }

    dataset = Dataset[VisualQAInputs, dict[str, Any], dict[str, Any]](
        name="clipping_studio_post_render_visual_qa",
        cases=[
            Case(
                name=inputs.clip_id,
                inputs=inputs,
                metadata={"universal_safe_box": list(UNIVERSAL_SAFE_BOX_9_16)},
            )
        ],
        evaluators=[
            DeterministicMediaEvaluator(),
            PackagingAntiSlopEvaluator(),
            VlmVisualCriticEvaluator(),
        ],
    )
    eval_report = dataset.evaluate_sync(_task, progress=False)
    if not eval_report.cases:
        failure_msg = eval_report.failures[0].error_message if eval_report.failures else "unknown_eval_failure"
        result = {
            "clip_id": inputs.clip_id,
            "passed": False,
            "status": "NEEDS_VISUAL_FIX",
            "recommended_fix": "none",
            "error": failure_msg,
            "assertions": {},
            "scores": {},
        }
        if out_report_json is not None:
            out_report_json.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        return result

    rc = eval_report.cases[0]
    assertions_out: dict[str, dict[str, Any]] = {
        k: {"passed": bool(v.value), "reason": v.reason}
        for k, v in rc.assertions.items()
    }
    scores_out: Mapping[str, float] = {
        k: round(float(v.value), 4)
        for k, v in rc.scores.items()
    }
    all_assertions_ok = bool(assertions_out) and all(a["passed"] for a in assertions_out.values()) and not rc.evaluator_failures
    task_out = rc.output or {}
    vlm_dict = task_out.get("vlm_verdict") or {}

    rec_fix: RecommendedFix = "none"
    if not all_assertions_ok:
        if not assertions_out.get("cover_not_duplicating_overlay", {}).get("passed", True):
            rec_fix = "alternate_cover_hook"
        elif vlm_dict.get("recommended_parametric_fix") in {"shift_cover_frame", "alternate_cover_hook", "shift_overlay_y"}:
            rec_fix = vlm_dict["recommended_parametric_fix"]
        elif not assertions_out.get("vlm_face_unobstructed", {"passed": True}).get("passed", True):
            rec_fix = "shift_cover_frame"
        elif not assertions_out.get("vlm_hud_buttons_clear", {"passed": True}).get("passed", True):
            rec_fix = "shift_overlay_y"

    summary: dict[str, Any] = {
        "clip_id": inputs.clip_id,
        "video": str(inputs.video_path),
        "cover": str(inputs.cover_path) if inputs.cover_path else None,
        "passed": all_assertions_ok,
        "status": "READY_HITL" if all_assertions_ok else "NEEDS_VISUAL_FIX",
        "recommended_fix": rec_fix,
        "vlm_provider": task_out.get("vlm_provider", "skipped"),
        "assertions": assertions_out,
        "scores": dict(scores_out),
        "vlm_verdict": vlm_dict or None,
        "media_check": task_out.get("media_check"),
        "inspected_images": task_out.get("inspected_images", []),
    }
    if out_report_json is not None:
        out_report_json.parent.mkdir(parents=True, exist_ok=True)
        out_report_json.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Clipping Studio Autonomous Post-Render Visual QA (pydantic-evals)")
    parser.add_argument("video", help="Path to rendered 9:16 master MP4")
    parser.add_argument("--cover", default=None, help="Optional path to 9:16 cover JPG")
    parser.add_argument("--packaging", default=None, help="Optional path to *_packaging.json")
    parser.add_argument("--state", default=None, help="Optional path to state.json to auto-read overlay callout_text")
    parser.add_argument("--clip-id", default=None, help="Clip ID (inferred from video stem if omitted)")
    parser.add_argument("--expected-fps", type=float, default=60.0, help="Expected CFR FPS (default: 60.0)")
    parser.add_argument("--no-vlm", action="store_true", help="Run deterministic evaluators only")
    parser.add_argument("--out", default=None, help="Optional output JSON report path")
    args = parser.parse_args()

    vid = Path(args.video)
    cid = args.clip_id or vid.stem.replace("_master_9_16", "")
    cov = Path(args.cover) if args.cover else (vid.parent / f"{cid}_cover_9_16.jpg")
    if not cov.is_file():
        cov = None

    pkg_path = Path(args.packaging) if args.packaging else (vid.parent / f"{cid}_packaging.json")
    shorts_title, cover_hl = "", ""
    if pkg_path.is_file():
        try:
            pk = json.loads(pkg_path.read_text(encoding="utf-8"))
            shorts_title = str(pk.get("shorts_title") or "")
            cover_hl = str(pk.get("suggested_on_screen_hook") or "")
        except Exception:
            pass

    state_path = Path(args.state) if args.state else (vid.parent / "state.json")
    callout_txt = ""
    if state_path.is_file():
        try:
            st = json.loads(state_path.read_text(encoding="utf-8"))
            for ep in st.get("edit_plans") or []:
                if ep.get("clip_id") == cid and ep.get("freeze_frame"):
                    callout_txt = str(ep["freeze_frame"].get("callout_text") or "")
                    break
        except Exception:
            pass

    out_json = Path(args.out) if args.out else (vid.parent / f"{cid}_visual_qa.json")
    report = evaluate_visual_qa(
        VisualQAInputs(
            clip_id=cid,
            video_path=vid,
            cover_path=cov,
            expected_fps=args.expected_fps if args.expected_fps > 0 else None,
            shorts_title=shorts_title,
            cover_headline=cover_hl,
            overlay_callout_text=callout_txt,
            run_vlm=not bool(args.no_vlm),
        ),
        out_report_json=out_json,
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
