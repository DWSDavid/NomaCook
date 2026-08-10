"""Offline video eval: MP4 → perception → StateEngine → JSONL evidence + snapshots + latency.

Usage:
    .venv/bin/python harness/eval_tomato_to_fridge.py --source data/test_videos/demo.mp4 \
        --run-dir data/evals/run_01
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from perception.detector import ObjectDetector
from perception.fusion import InteractionTracker
from perception.hands import HandTracker
from perception.tomato_to_fridge_events import (
    TomatoToFridgeTracker,
    canonicalize_detections,
)
from server.engine import StateEngine, load_recipe
from server.engine.snapshot import build_task_snapshot
from server.events import create_event
from server.live.frame_source import open_source
from server.pipeline.session import SESSION_EPOCH, event_id_for, t_server_for

SESSION_ID = "ses_tomato_fridge_eval"

TOMATO_FRIDGE_VOCAB = [
    "tomato", "cherry tomato", "red fruit",
    "refrigerator", "fridge", "freezer",
    "table", "kitchen counter", "desk",
    "hand", "person hand",
]


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", required=True, help="video file path")
    ap.add_argument("--run-dir", required=True, help="output directory")
    ap.add_argument("--device", default="cpu", help="yolo device")
    ap.add_argument("--detect-every", type=int, default=3)
    ap.add_argument("--conf", type=float, default=0.10)
    ap.add_argument("--table-fraction", type=float, default=0.70)
    ap.add_argument("--stability", type=int, default=3)
    ap.add_argument("--max-frames", type=int, default=0)
    return ap


def _to_xyxy(box) -> tuple[int, int, int, int]:
    return (int(box[0]), int(box[1]), int(box[2]), int(box[3]))


def _palm(p) -> tuple[float, float]:
    return (float(p[0]), float(p[1]))


def run(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    recipe = load_recipe("sop/tomato_to_fridge.json")
    engine = StateEngine(session_id=SESSION_ID, recipe=recipe, started_at=SESSION_EPOCH)
    detector = ObjectDetector(device=args.device, conf=args.conf)
    detector.set_vocab(TOMATO_FRIDGE_VOCAB)
    hand_tracker = HandTracker()
    fusion = InteractionTracker(k_frames=3)

    source = open_source(args.source)
    w = getattr(source, "width", 640)
    h = getattr(source, "height", 480)

    task_tracker = TomatoToFridgeTracker(
        frame_width=w, frame_height=h,
        table_fraction=args.table_fraction,
        stability_frames=args.stability,
    )

    events_path = run_dir / "events.jsonl"
    snapshots_path = run_dir / "snapshots.jsonl"
    latency_path = run_dir / "latency.csv"

    seq = 0
    frame_idx = 0
    total_event_count = 0
    yolo_latencies: list[tuple[int, float]] = []
    frame_latencies: list[tuple[int, float]] = []

    def emit(envelope) -> None:
        nonlocal total_event_count
        engine.consume(envelope)
        total_event_count += 1
        with events_path.open("a", encoding="utf-8") as f:
            f.write(envelope.model_dump_json() + "\n")
        snap = build_task_snapshot(engine.context, engine.current_step)
        with snapshots_path.open("a", encoding="utf-8") as f:
            f.write(snap.model_dump_json() + "\n")

    print(f"source={args.source}  device={args.device}  detect_every={args.detect_every}")
    print(f"run_dir={run_dir}")
    print(f"Recipe: {recipe.dish} ({len(recipe.steps)} steps)")

    try:
        for pts_ms, frame in source.frames():
            t0 = time.monotonic()
            inference_ran = frame_idx % args.detect_every == 0

            yolo_ms = 0.0
            raw_dets = []
            if inference_ran:
                t_yolo = time.monotonic()
                raw_dets = detector.detect(frame)
                yolo_ms = (time.monotonic() - t_yolo) * 1000.0
                yolo_latencies.append((frame_idx, yolo_ms))

            hands = hand_tracker.detect(frame, timestamp_ms=pts_ms)
            hand_data = [(h.handedness, _palm(h.palm_center), _to_xyxy(h.box), h.is_gripping)
                         for h in hands]

            can_dets = canonicalize_detections(
                [(d.label, d.confidence, _to_xyxy(d.box)) for d in raw_dets]
            )

            if inference_ran and can_dets:
                fusion_events = fusion.update(
                    t=pts_ms / 1000.0, frame=frame_idx,
                    hands=[(h.handedness, _palm(h.palm_center), _to_xyxy(h.box), h.is_gripping)
                           for h in hands],
                    detections=[(d[0], d[1], d[2]) for d in can_dets],
                )
                ie_names = [(ev.event, ev.hand, ev.object) for ev in fusion_events]

                task_events = task_tracker.update(
                    t_ms=pts_ms,
                    detections=can_dets,
                    hands=hand_data,
                    interaction_events=ie_names,
                )

                for tev in task_events:
                    emit(create_event(
                        session_id=SESSION_ID, seq=seq,
                        event_type=tev.event_type, t_device_ms=pts_ms,
                        t_server_est=t_server_for(pts_ms),
                        received_at=t_server_for(pts_ms),
                        source="tomato_fridge_geometry_v1",
                        event_id=event_id_for(SESSION_ID, seq),
                        confidence=tev.confidence,
                        payload=tev.payload,
                    ))
                    seq += 1

            frame_ms = (time.monotonic() - t0) * 1000.0
            frame_latencies.append((frame_idx, frame_ms))

            frame_idx += 1
            if frame_idx % 100 == 0:
                print(f"  frame {frame_idx}  events={total_event_count}  "
                      f"step={engine.context.current_step_id}  "
                      f"score={engine.context.step_progress.score:.2f}")
            if args.max_frames and frame_idx >= args.max_frames:
                break

    finally:
        source.close()
        hand_tracker.close()

    # ── write latency CSV ──
    with latency_path.open("w", newline="") as f:
        wtr = csv.writer(f)
        wtr.writerow(["frame_idx", "total_ms"])
        for fi, ms in frame_latencies:
            wtr.writerow([fi, round(ms, 2)])

    # ── write summary ──
    ctx = engine.context
    summary = {
        "session_id": SESSION_ID,
        "recipe": recipe.dish,
        "recipe_version_id": recipe.recipe_version_id,
        "total_frames": frame_idx,
        "total_events": total_event_count,
        "final_step_id": ctx.current_step_id,
        "step_status": ctx.step_status,
        "final_score": ctx.step_progress.score,
        "context_version": ctx.context_version,
        "yolo_calls": len(yolo_latencies),
        "yolo_mean_ms": round(
            sum(ms for _, ms in yolo_latencies) / len(yolo_latencies), 2
        ) if yolo_latencies else 0,
        "frame_mean_ms": round(
            sum(ms for _, ms in frame_latencies) / len(frame_latencies), 2
        ) if frame_latencies else 0,
    }
    with (run_dir / "summary.json").open("w") as f:
        json.dump(summary, f, indent=2)

    print(f"\ndone. frames={frame_idx}  events={total_event_count}")
    print(f"  final step: {ctx.current_step_id}  status: {ctx.step_status}")
    print(f"  score: {ctx.step_progress.score:.2f}")
    if yolo_latencies:
        print(f"  yolo: n={len(yolo_latencies)}  "
              f"mean={summary['yolo_mean_ms']}ms  "
              f"p95={sorted(ms for _, ms in yolo_latencies)[int(len(yolo_latencies) * 0.95)]:.1f}ms")
    print(f"  frame mean: {summary['frame_mean_ms']}ms")


if __name__ == "__main__":
    run(build_parser().parse_args())
