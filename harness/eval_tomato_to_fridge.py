"""Offline video eval: MP4 → perception → StateEngine → artifacts.

Produces (always, even if empty):
  events.jsonl       — evidence events via EventLog
  snapshots.jsonl    — TaskSnapshot after every event
  observations.jsonl — per-inference-frame perception details
  latency.csv        — per-frame timing
  summary.json       — aggregate stats

Usage:
    .venv/bin/python -m harness.eval_tomato_to_fridge \
        --source data/test_videos/demo.mp4 --run-dir data/evals/run_01
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from perception.detector import ObjectDetector
from perception.fusion import InteractionTracker
from perception.hands import HandTracker
from perception.tomato_to_fridge_events import (
    TomatoToFridgeTracker,
    canonicalize_detections,
)
from server.domain.config import DomainConfig
from server.engine import StateEngine, load_recipe
from server.engine.snapshot import build_task_snapshot
from server.events import create_event
from server.events.log import EventLog
from server.live.frame_source import open_source
from server.pipeline.session import (
    SESSION_EPOCH,
    SessionPaths,
    create_run_dir,
    event_id_for,
    t_server_for,
)

SESSION_ID = "ses_tomato_fridge_eval"
DOMAIN_PACK = Path(__file__).resolve().parent.parent / "domain_packs" / "kitchen" / "tomato_to_fridge.yaml"


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", required=True, help="video file path")
    ap.add_argument("--run-dir", required=True, help="output directory")
    ap.add_argument("--device", default=None, help="yolo device (default: from domain pack)")
    ap.add_argument("--detect-every", type=int, default=None)
    ap.add_argument("--conf", type=float, default=None)
    ap.add_argument("--table-fraction", type=float, default=None)
    ap.add_argument("--stability", type=int, default=None)
    ap.add_argument("--max-frames", type=int, default=0)
    return ap


def _to_xyxy(box) -> tuple[int, int, int, int]:
    return (int(box[0]), int(box[1]), int(box[2]), int(box[3]))


def run(args: argparse.Namespace) -> None:
    cfg = DomainConfig.load(DOMAIN_PACK)
    recipe = load_recipe("sop/tomato_to_fridge.json")

    # ── output directory ──
    run_dir = Path(args.run_dir)
    run_id = run_dir.name
    base = run_dir.parent
    sid = f"ses_tomato_fridge"
    paths = SessionPaths(root=run_dir)
    paths.root.mkdir(parents=True, exist_ok=True)
    (paths.root / "keyframes").mkdir(exist_ok=True)
    if paths.events.exists() and paths.events.stat().st_size > 0:
        raise FileExistsError(f"events.jsonl already exists in {run_dir} — refusing to overwrite")

    device = args.device or cfg.detector_device
    conf = args.conf if args.conf is not None else cfg.detector_conf
    detect_every = args.detect_every or cfg.detect_every
    table_frac = args.table_fraction if args.table_fraction is not None else cfg.table_fraction
    stability = args.stability or cfg.stability_frames

    engine = StateEngine(session_id=SESSION_ID, recipe=recipe, started_at=SESSION_EPOCH)
    detector = ObjectDetector(device=device, conf=conf)
    detector.set_vocab(cfg.vocab)
    hand_tracker = HandTracker()
    fusion = InteractionTracker(k_frames=3)
    source = open_source(args.source)
    w = getattr(source, "width", 640)
    h = getattr(source, "height", 480)

    fridge_fallback = (
        int(cfg.fridge_fallback[0] * w), int(cfg.fridge_fallback[1] * h),
        int(cfg.fridge_fallback[2] * w), int(cfg.fridge_fallback[3] * h),
    )

    task_tracker = TomatoToFridgeTracker(
        frame_width=w, frame_height=h,
        table_fraction=table_frac, stability_frames=stability,
    )
    task_tracker._fridge_fallback = fridge_fallback

    events_log = EventLog(paths.events)
    paths.events.touch()  # ensure exists even with 0 events
    obs_path = paths.root / "observations.jsonl"
    snap_path = paths.root / "snapshots.jsonl"
    latency_path = paths.root / "latency.csv"

    # Ensure empty artifacts exist
    for p in (obs_path, snap_path, latency_path):
        p.touch()

    seq = 0
    frame_idx = 0
    total_events = 0
    event_counts: dict[str, int] = {}
    yolo_latencies: list[float] = []
    hand_latencies: list[float] = []
    state_latencies: list[float] = []
    total_latencies: list[float] = []

    with latency_path.open("w", newline="") as lf:
        lw = csv.writer(lf)
        lw.writerow(["frame_idx", "inference_ran", "yolo_ms", "hand_ms", "state_update_ms", "total_ms"])

    def emit(envelope) -> None:
        nonlocal total_events
        t0 = time.monotonic()
        engine.consume(envelope)
        state_latencies.append((time.monotonic() - t0) * 1000.0)
        total_events += 1
        event_counts[envelope.type] = event_counts.get(envelope.type, 0) + 1
        events_log.append(envelope)
        snap = build_task_snapshot(engine.context, engine.current_step)
        with snap_path.open("a", encoding="utf-8") as f:
            f.write(snap.model_dump_json() + "\n")

    print(f"source={args.source}  device={device}  detect_every={detect_every}")
    print(f"run_dir={run_dir}")
    print(f"Recipe: {recipe.dish} ({len(recipe.steps)} steps)")

    try:
        for pts_ms, frame in source.frames():
            t0 = time.monotonic()
            inference_ran = frame_idx % detect_every == 0

            yolo_ms = 0.0
            raw_dets = []
            if inference_ran:
                t_yolo = time.monotonic()
                raw_dets = detector.detect(frame)
                yolo_ms = (time.monotonic() - t_yolo) * 1000.0
                yolo_latencies.append(yolo_ms)

            t_hand = time.monotonic()
            hands = hand_tracker.detect(frame, timestamp_ms=pts_ms)
            hand_ms = (time.monotonic() - t_hand) * 1000.0
            hand_latencies.append(hand_ms)

            hand_data = [(h.handedness,
                          (float(h.palm_center[0]), float(h.palm_center[1])),
                          _to_xyxy(h.box), h.is_gripping) for h in hands]

            can_dets = canonicalize_detections(
                [(d.label, d.confidence, _to_xyxy(d.box)) for d in raw_dets]
            )

            emitted_types: list[str] = []
            # ── always update trackers on inference frames (even with empty dets) ──
            if inference_ran:
                fusion_events = fusion.update(
                    t=pts_ms / 1000.0, frame=frame_idx,
                    hands=[(h.handedness,
                            (float(h.palm_center[0]), float(h.palm_center[1])),
                            _to_xyxy(h.box), h.is_gripping) for h in hands],
                    detections=[(d[0], d[1], d[2]) for d in can_dets],
                )
                ie_names = [(ev.event, ev.hand, ev.object) for ev in fusion_events]

                task_events = task_tracker.update(
                    t_ms=pts_ms, detections=can_dets, hands=hand_data,
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
                        confidence=tev.confidence, payload=tev.payload,
                    ))
                    seq += 1
                    emitted_types.append(tev.event_type)

            # ── observations.jsonl ──
            obs_row = {
                "frame_idx": frame_idx,
                "pts_ms": round(pts_ms, 2),
                "inference_ran": inference_ran,
                "current_step_id": engine.current_step.id,
                "raw_detections": [
                    {"label": d.label, "conf": round(d.confidence, 3),
                     "box": _to_xyxy(d.box)}
                    for d in raw_dets
                ],
                "canonical_detections": [
                    {"label": d[0], "conf": round(d[1], 3), "box": d[2]}
                    for d in can_dets
                ],
                "hand_count": len(hands),
                "hands_gripping": sum(1 for h in hands if h.is_gripping),
                "emitted_event_types": emitted_types,
            }
            with obs_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(obs_row, ensure_ascii=False) + "\n")

            total_ms = (time.monotonic() - t0) * 1000.0
            total_latencies.append(total_ms)

            with latency_path.open("a", newline="") as lf:
                lw = csv.writer(lf)
                lw.writerow([frame_idx, int(inference_ran),
                             round(yolo_ms, 2), round(hand_ms, 2),
                             round(state_latencies[-1], 2) if state_latencies else 0,
                             round(total_ms, 2)])

            frame_idx += 1
            if frame_idx % 100 == 0:
                print(f"  frame {frame_idx}  events={total_events}  "
                      f"step={engine.context.current_step_id}  "
                      f"score={engine.context.step_progress.score:.2f}")
            if args.max_frames and frame_idx >= args.max_frames:
                break

    finally:
        source.close()
        hand_tracker.close()

    def _pctl(values, pct):
        if not values:
            return 0
        s = sorted(values)
        k = (len(s) - 1) * pct / 100.0
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            return s[int(k)]
        return s[f] * (c - k) + s[c] * (k - f)

    ctx = engine.context
    summary = {
        "source": args.source,
        "task_id": cfg.task_id,
        "perception": {
            "detector_device": device,
            "detector_conf": conf,
            "detect_every": detect_every,
            "table_fraction": table_frac,
            "stability_frames": stability,
            "fridge_fallback": list(fridge_fallback),
        },
        "total_frames": frame_idx,
        "inference_count": len(yolo_latencies),
        "total_events": total_events,
        "event_type_counts": event_counts,
        "final_step_id": ctx.current_step_id,
        "step_status": ctx.step_status,
        "final_score": round(ctx.step_progress.score, 3),
        "context_version": ctx.context_version,
        "latency_yolo_mean_ms": round(sum(yolo_latencies) / len(yolo_latencies), 2) if yolo_latencies else 0,
        "latency_yolo_p95_ms": round(_pctl(yolo_latencies, 95), 2),
        "latency_state_mean_ms": round(sum(state_latencies) / len(state_latencies), 2) if state_latencies else 0,
        "latency_state_p95_ms": round(_pctl(state_latencies, 95), 2),
        "latency_total_mean_ms": round(sum(total_latencies) / len(total_latencies), 2) if total_latencies else 0,
        "latency_total_p95_ms": round(_pctl(total_latencies, 95), 2),
    }
    with (paths.root / "summary.json").open("w") as f:
        json.dump(summary, f, indent=2)

    print(f"\ndone. frames={frame_idx}  events={total_events}")
    print(f"  final step: {ctx.current_step_id}  status: {ctx.step_status}")
    print(f"  score: {ctx.step_progress.score:.2f}")
    if yolo_latencies:
        print(f"  yolo: n={len(yolo_latencies)}  mean={summary['latency_yolo_mean_ms']}ms  "
              f"p95={summary['latency_yolo_p95_ms']}ms")
    print(f"  total mean: {summary['latency_total_mean_ms']}ms  "
          f"p95: {summary['latency_total_p95_ms']}ms")


if __name__ == "__main__":
    run(build_parser().parse_args())
