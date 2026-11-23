"""Resumable Clipping Studio orchestrator CLI (`run` and `curate` via runs/<run_id>/state.json, Hito 1.5)."""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from evals.media_check import evaluate_video
from src.curator import curate_run
from src.miner import RUNS_DIR, enforce_runs_quota, mine_vod_slices, probe_kick_channel
from src.renderer import render_master_clip
from src.schemas import RunState
from src.transcriber import transcribe_run

TELEMETRY_PATH = RUNS_DIR / "telemetry.jsonl"


def _log_telemetry(event: dict[str, object]) -> None:
    TELEMETRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with TELEMETRY_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def _render_and_verify(run_dir: Path, state: RunState) -> RunState:
    compute_profile = os.getenv("COMPUTE_PROFILE", "local_hp").strip() or "local_hp"
    cand_map = {c.slice_id: c for c in state.candidates}
    tr_map = {t.slice_id: t for t in state.transcripts}
    rendered: list[str] = []

    for plan in state.edit_plans:
        cand = cand_map[plan.slice_id]
        tr = tr_map[plan.slice_id]
        out_mp4 = run_dir / f"{plan.clip_id}_master_9_16.mp4"
        render_master_clip(Path(cand.slice_video_path), plan, tr, out_mp4, compute_profile=compute_profile)

        qa_dir = run_dir / f"{plan.clip_id}_qa_frames"
        verdict = evaluate_video(out_mp4, qa_dir=qa_dir)
        (run_dir / f"{plan.clip_id}_media_check.json").write_text(json.dumps(verdict, indent=2), encoding="utf-8")
        rendered.append(str(out_mp4))
        print(
            f"[MEDIA_CHECK] {out_mp4.name}: passed={verdict['passed']} "
            f"lufs={verdict['checks']['loudness_ebur128'].get('integrated_lufs')} "
            f"dbtp={verdict['checks']['loudness_ebur128'].get('true_peak_dbtp')}"
        )

    state.rendered_clips = rendered
    state.stage = "reviewed" if rendered else state.stage
    state.disk_usage_mb = enforce_runs_quota(RUNS_DIR, protect_dir=run_dir)
    state.updated_at = datetime.now(UTC).isoformat()
    (run_dir / "state.json").write_text(state.model_dump_json(indent=2), encoding="utf-8")
    return state


def cmd_run(args: argparse.Namespace) -> int:
    t0 = time.perf_counter()
    run_id = args.run_id or datetime.now(UTC).strftime("run_%Y%m%d_%H%M%S")
    run_dir = RUNS_DIR / run_id
    state_path = run_dir / "state.json"

    if args.probe_kick:
        kp = probe_kick_channel(args.probe_kick)
        print(f"[KICK_PROBE] {json.dumps(kp)}")

    stage_order = {"initialized": 0, "mined": 1, "transcribed": 2, "curated": 3, "rendered": 4, "reviewed": 5}
    cur_stage = 0
    if state_path.exists() and not args.fresh:
        prev = RunState.model_validate_json(state_path.read_text(encoding="utf-8"))
        cur_stage = stage_order.get(prev.stage, 0)

    if cur_stage < 1:
        state = mine_vod_slices(
            args.url,
            streamer_id=args.streamer,
            run_id=run_id,
            top_k=args.top_k,
            allow_short=args.allow_short,
            allow_trailer=args.allow_trailer,
        )
    else:
        state = RunState.model_validate_json(state_path.read_text(encoding="utf-8"))

    if cur_stage < 2:
        state = transcribe_run(run_dir)

    if cur_stage < 3:
        state = curate_run(run_dir)

    if state.edit_plans:
        state = _render_and_verify(run_dir, state)
    else:
        print(f"[CLI] run_id={run_id} finished without approved clips (check slice_audits/critic_results).")

    elapsed = round(time.perf_counter() - t0, 2)
    _log_telemetry({
        "ts": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "url": args.url,
        "streamer": args.streamer,
        "stage": state.stage,
        "approved_clips": len(state.edit_plans),
        "rendered_clips": len(state.rendered_clips),
        "disk_usage_mb": state.disk_usage_mb,
        "elapsed_sec": elapsed,
    })
    print(f"[CLI] run_id={run_id} stage={state.stage} rendered={len(state.rendered_clips)} disk_mb={state.disk_usage_mb} elapsed_sec={elapsed}")
    return 0


def cmd_curate(args: argparse.Namespace) -> int:
    run_dir = RUNS_DIR / args.run_id
    if args.retranscribe:
        transcribe_run(run_dir)
    state = curate_run(run_dir)
    if args.render and state.edit_plans:
        state = _render_and_verify(run_dir, state)
    print(f"[CLI_CURATE] run_id={args.run_id} audits={len(state.slice_audits)} plans={len(state.edit_plans)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Clipping Studio Orchestrator CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Execute resumable end-to-end pipeline")
    p_run.add_argument("--url", required=True)
    p_run.add_argument("--streamer", default="default")
    p_run.add_argument("--run-id", default=None)
    p_run.add_argument("--top-k", type=int, default=2)
    p_run.add_argument("--allow-short", action="store_true")
    p_run.add_argument("--allow-trailer", action="store_true")
    p_run.add_argument("--probe-kick", default=None)
    p_run.add_argument("--fresh", action="store_true", help="Ignore existing state.json checkpoint")
    p_run.set_defaults(func=cmd_run)

    p_cur = sub.add_parser("curate", help="Re-run transcriber/curator on an existing run")
    p_cur.add_argument("--run-id", required=True)
    p_cur.add_argument("--retranscribe", action="store_true")
    p_cur.add_argument("--render", action="store_true")
    p_cur.set_defaults(func=cmd_curate)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
