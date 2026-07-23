"""Assemble the full offline pipeline: MP4 -> perception -> envelopes ->
EventLog -> StateEngine -> keyframes/timeline (-> render/VLM added later).

Usage:
    .venv/bin/python harness/run_pipeline.py --source data/test_videos/x.mp4
    .venv/bin/python harness/run_pipeline.py --source x.mp4 --device cpu \
        --script tests/fixtures/tomato_egg_full_script.json --run-tag a
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from perception.detector import ObjectDetector
from perception.fusion import InteractionTracker
from perception.hands import HandTracker
from server.engine import StateEngine, load_recipe
from server.events.log import EventLog
from server.perception import (
    ContextualVocabularyController,
    build_detection_context,
    canonicalize_detections,
    extract_tomato_egg_color_signals,
)
from server.pipeline.evidence import (
    interaction_event,
    load_script,
    objects_present_event,
    presence_states,
    roi_color_event,
    scripted_event,
)
from server.pipeline.session import (
    SESSION_EPOCH,
    create_run_dir,
    session_id_for,
)
from server.pipeline.render import AnnotatedVideoWriter, draw_overlay
from server.pipeline.timeline import (
    KeyframeSampler,
    StateSnapshot,
    append_jsonl,
    diff_snapshots,
    keyframe_row,
)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", required=True, help="video file path (not webcam)")
    ap.add_argument("--sop", default="sop/tomato_egg.json")
    ap.add_argument("--device", default="cpu", help="yolo device; cpu for determinism")
    ap.add_argument("--detect-every", type=int, default=3)
    ap.add_argument("--keyframe-interval", type=float, default=3.0, help="seconds")
    ap.add_argument("--script", default=None, help="scripted evidence JSON")
    ap.add_argument("--run-tag", default=None)
    ap.add_argument("--max-frames", type=int, default=0)
    ap.add_argument("--k-frames", type=int, default=3, help="fusion debounce")
    ap.add_argument("--render", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--vlm", choices=["off", "gemini"], default="off")
    return ap


def run(args: argparse.Namespace) -> dict:
    video = Path(args.source)
    if not video.is_file():
        raise SystemExit(f"--source must be an existing video file: {video}")
    recipe = load_recipe(args.sop)
    session_id = session_id_for(video, recipe.recipe_version_id)
    paths = create_run_dir(session_id, run_tag=args.run_tag)
    print(f"session={session_id}\nrun dir={paths.root}")

    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        raise SystemExit(f"cannot open video {video}")
    fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    frame_ms = 1000.0 / fps

    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    writer = (AnnotatedVideoWriter(paths.annotated, fps=fps,
                                   frame_size=(width, height))
              if args.render else None)
    recent_event_texts: list[str] = []

    engine = StateEngine(
        session_id=session_id, recipe=recipe, started_at=SESSION_EPOCH
    )
    log = EventLog(paths.events)
    detector = ObjectDetector(device=args.device, conf=0.10)
    controller = ContextualVocabularyController(detector)
    det_ctx = build_detection_context(engine.context, recipe)
    controller.sync(det_ctx)
    hand_tracker = HandTracker()
    fusion = InteractionTracker(k_frames=args.k_frames)
    confirmer = None
    if args.vlm == "gemini":
        from server.pipeline.vlm_hook import VLMConfirmer
        from server.vlm.client import GeminiVLMClient

        confirmer = VLMConfirmer(GeminiVLMClient())
    sampler = KeyframeSampler(interval_ms=args.keyframe_interval * 1000.0)
    script = load_script(args.script) if args.script else []
    script_cursor = 0

    seq = 0
    frame_idx = 0
    latest_canon = []
    color_state: str | None = None
    prev_snapshot: StateSnapshot | None = None
    transitions: list[dict] = []
    wall_start = time.perf_counter()

    def emit(envelope) -> None:
        nonlocal seq, det_ctx
        if envelope.type == "perception.hand_object_relation":
            recent_event_texts.append(
                f"{envelope.payload.get('relation')} "
                f"{envelope.payload.get('hand')}/{envelope.payload.get('object_class')}")
        log.append(envelope)
        result = engine.consume(envelope)
        seq += 1
        if result.transition is not None:
            transitions.append(
                {
                    "decision_id": result.transition.decision_id,
                    "completed_step_id": result.transition.completed_step_id,
                    "next_step_id": result.transition.next_step_id,
                    "score": result.transition.score,
                    "pts_ms": envelope.t_device_ms,
                }
            )
            print(
                f"[{envelope.t_device_ms:8.0f}ms] STEP DONE "
                f"{result.transition.completed_step_id} -> "
                f"{result.transition.next_step_id or 'SESSION COMPLETE'}"
            )
            if result.transition.next_step_id is not None:
                det_ctx = build_detection_context(engine.context, recipe)
                controller.sync(det_ctx)
        elif result.status == "question_pending" and engine.context.pending_question:
            print(
                f"[{envelope.t_device_ms:8.0f}ms] QUESTION: "
                f"{engine.context.pending_question.question}"
            )

    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            pts_ms = frame_idx * frame_ms
            step_id = engine.context.current_step_id

            if frame_idx % args.detect_every == 0:
                raw = detector.detect(frame)
                latest_canon = canonicalize_detections(raw, det_ctx)
            hands = hand_tracker.detect(frame, timestamp_ms=pts_ms)

            for ev in fusion.update(
                t=pts_ms / 1000.0,
                frame=frame_idx,
                hands=[
                    (h.handedness, h.palm_center, h.box, h.is_gripping)
                    for h in hands
                ],
                detections=[
                    (d.canonical_label, d.conf, d.box) for d in latest_canon
                ],
            ):
                emit(interaction_event(ev, session_id=session_id, seq=seq))

            while (
                script_cursor < len(script)
                and script[script_cursor]["pts_ms"] <= pts_ms
            ):
                row = script[script_cursor]
                pending = engine.context.pending_question
                question_ref = pending.triggered_by_event_id if pending else None
                emit(
                    scripted_event(
                        row,
                        row["_index"],
                        session_id=session_id,
                        seq=seq,
                        question_event_id=question_ref,
                    )
                )
                script_cursor += 1

            if sampler.due(pts_ms):
                for state, conf in presence_states(step_id, latest_canon):
                    emit(
                        objects_present_event(
                            state,
                            conf,
                            session_id=session_id,
                            seq=seq,
                            step_id=step_id,
                            pts_ms=pts_ms,
                            frame_idx=frame_idx,
                        )
                    )
                wok = next(
                    (
                        d
                        for d in latest_canon
                        if d.canonical_label == "wok" and d.role == "primary"
                    ),
                    None,
                )
                if wok is not None:
                    signals = extract_tomato_egg_color_signals(frame, wok.box)
                    color_state = signals.state
                    emit(
                        roi_color_event(
                            signals,
                            session_id=session_id,
                            seq=seq,
                            step_id=step_id,
                            pts_ms=pts_ms,
                            frame_idx=frame_idx,
                        )
                    )
                snapshot = StateSnapshot(
                    pts_ms=pts_ms,
                    frame_idx=frame_idx,
                    step_id=engine.context.current_step_id,
                    context_version=engine.context.context_version,
                    score=engine.context.step_progress.score,
                    pending_question=(
                        engine.context.pending_question.question
                        if engine.context.pending_question
                        else None
                    ),
                    detections=tuple(
                        sorted(
                            (d.canonical_label, round(d.conf, 2))
                            for d in latest_canon
                        )
                    ),
                    color_state=color_state,
                )
                jpg_name = f"kf_{frame_idx:06d}_{int(pts_ms)}ms.jpg"
                cv2.imwrite(str(paths.keyframes_dir / jpg_name), frame)
                append_jsonl(
                    paths.timeline,
                    keyframe_row(
                        snapshot,
                        diff_snapshots(prev_snapshot, snapshot),
                        jpg_name,
                    ),
                )
                prev_snapshot = snapshot
                if confirmer is not None:
                    vlm_env = confirmer.maybe_confirm(
                        engine.context,
                        engine.current_step,
                        frame,
                        session_id=session_id,
                        seq=seq,
                        pts_ms=pts_ms,
                        frame_idx=frame_idx,
                    )
                    if vlm_env is not None:
                        emit(vlm_env)

            if writer is not None:
                step = engine.current_step
                draw_overlay(
                    frame,
                    detections=latest_canon,
                    step_id=engine.context.current_step_id,
                    instruction=step.instruction,
                    score=engine.context.step_progress.score,
                    threshold=step.completion_policy.threshold,
                    pending_question=(engine.context.pending_question.question
                                      if engine.context.pending_question else None),
                    recent_events=recent_event_texts,
                    color_text=(f"color={color_state}" if color_state else None),
                )
                writer.write(frame)

            frame_idx += 1
            if args.max_frames and frame_idx >= args.max_frames:
                break
    finally:
        capture.release()
        hand_tracker.close()
        writer and writer.close()

    meta = {
        "session_id": session_id,
        "video": str(video),
        "sop": args.sop,
        "fps": fps,
        "frames": frame_idx,
        "events": len(log),
        "transitions": transitions,
        "final_step_id": engine.context.current_step_id,
        "final_status": engine.context.step_status,
        "wall_seconds": round(time.perf_counter() - wall_start, 2),
        "annotated_frames": writer.frames_written if writer else 0,
        "vlm_mode": args.vlm,
        "args": {k: v for k, v in vars(args).items()},
    }
    paths.meta.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    from server.pipeline.report import write_report
    print(f"report -> {write_report(paths)}")
    print(
        f"frames={frame_idx} events={len(log)} "
        f"transitions={len(transitions)} final={meta['final_status']}"
    )
    return meta


def main() -> None:
    run(build_parser().parse_args())


if __name__ == "__main__":
    main()
