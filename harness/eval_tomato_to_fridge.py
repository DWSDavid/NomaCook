"""Offline video eval with optional annotated rendering and ground-truth comparison.

Artifacts always produced:
  events.jsonl, snapshots.jsonl, observations.jsonl, latency.csv, summary.json

With --render-video: annotated.mp4
With --annotations <yaml>: comparison.jsonl + AI-vs-Expected overlay

Annotations are NEVER fed to StateEngine — overlay only.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from perception.detector import ObjectDetector
from perception.fusion import InteractionTracker
from perception.hands import HandTracker
from perception.tomato_to_fridge_events import (
    TomatoToFridgeTracker,
    canonicalize_detections,
)
from server.domain.annotations import AnnotationTimeline, load_annotations
from server.domain.config import DomainConfig
from server.engine import StateEngine, load_recipe
from server.engine.snapshot import build_task_snapshot
from server.events import create_event
from server.events.log import EventLog
from server.live.frame_source import open_source
from server.pipeline.render import (
    AnnotatedVideoWriter,
    HAND_COLOR,
    HAND_EDGES,
)
from server.pipeline.narrate import narrate_run
from server.pipeline.session import (
    SESSION_EPOCH,
    SessionPaths,
    event_id_for,
    t_server_for,
)

SESSION_ID = "ses_tomato_fridge_eval"
DOMAIN_PACK = Path(__file__).resolve().parent.parent / "domain_packs" / "kitchen" / "tomato_to_fridge.yaml"
_FONT = cv2.FONT_HERSHEY_SIMPLEX

_STEP_LABELS = {
    "ready": ("任务已开始", "Task started"),
    "tomato_on_table": ("番茄在桌上", "Tomato on the table"),
    "hand_near_tomato": ("手已靠近番茄", "Hand near the tomato"),
    "tomato_held": ("已拿起番茄", "Tomato picked up"),
    "tomato_in_transit": ("正在运送番茄", "Moving the tomato"),
    "fridge_interaction": ("已到达冰箱", "At the refrigerator"),
    "candidate_inside_fridge": ("番茄正在放入冰箱", "Tomato entering the refrigerator"),
    "tomato_released_inside": ("番茄已在冰箱内放稳", "Tomato stable in the refrigerator"),
}

_NEXT_INSTRUCTIONS = {
    "ready": ("请准备开始任务。", "Get ready to begin."),
    "tomato_on_table": ("请确认番茄在桌面上。", "Confirm the tomato is on the table."),
    "hand_near_tomato": ("请把手靠近番茄。", "Move your hand toward the tomato."),
    "tomato_held": ("请拿起番茄。", "Pick up the tomato."),
    "tomato_in_transit": ("请把番茄移向冰箱。", "Move the tomato toward the refrigerator."),
    "fridge_interaction": ("请把番茄带到冰箱前并打开冰箱。", "Bring the tomato to the refrigerator and open it."),
    "candidate_inside_fridge": ("请把番茄放进冰箱。", "Place the tomato inside the refrigerator."),
    "tomato_released_inside": ("请松手，并让番茄稳定放好。", "Release the tomato and let it settle."),
}

_SPEECH_BY_STEP = {
    "tomato_on_table": "番茄已定位。下一步，请拿起番茄。",
    "tomato_held": "已经拿起番茄。下一步，请移向冰箱。",
    "fridge_interaction": "已经到达冰箱。下一步，请打开冰箱。",
    "candidate_inside_fridge": "冰箱已经打开。下一步，请把番茄放进去。",
    "tomato_released_inside": "番茄已经稳定放入冰箱，任务完成。",
}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", required=True, help="video file path")
    ap.add_argument("--run-dir", required=True, help="output directory")
    ap.add_argument("--device", default=None, help="yolo device (default: from domain pack)")
    ap.add_argument("--detect-every", type=int, default=None)
    ap.add_argument("--conf", type=float, default=None)
    ap.add_argument("--table-fraction", type=float, default=None)
    ap.add_argument("--stability", type=int, default=None)
    ap.add_argument("--annotations", default=None, help="annotation YAML for comparison")
    ap.add_argument("--render-video", action="store_true",
                    help="output annotated.mp4 with AI vs Expected overlay")
    ap.add_argument("--local-narration", action="store_true",
                    help="add local macOS Chinese narration to the rendered video")
    ap.add_argument("--voice", default="Meijia",
                    help="macOS say voice used by --local-narration")
    ap.add_argument("--max-frames", type=int, default=0)
    return ap


def _to_xyxy(box) -> tuple[int, int, int, int]:
    return (int(box[0]), int(box[1]), int(box[2]), int(box[3]))


def _recognized_step_id(previous, engine_result):
    if engine_result.transition is not None:
        return engine_result.transition.completed_step_id
    return previous or engine_result.context.current_step_id


def _presentation_for(
    recognized_step_id: str,
    next_step_id: str | None,
) -> dict[str, str]:
    recognized_zh, recognized_en = _STEP_LABELS[recognized_step_id]
    if next_step_id is None:
        next_zh, next_en = "任务完成。", "Task complete."
    else:
        next_zh, next_en = _NEXT_INSTRUCTIONS[next_step_id]
    return {
        "recognized_zh": recognized_zh,
        "recognized_en": recognized_en,
        "next_zh": next_zh,
        "next_en": next_en,
    }


def _local_narration_items(transitions: list[dict]) -> list[dict]:
    return [
        {
            "pts_ms": transition["pts_ms"],
            "kind": (
                "complete"
                if transition["step_id"] == "tomato_released_inside"
                else "step"
            ),
            "step_id": transition["step_id"],
            "text": _SPEECH_BY_STEP[transition["step_id"]],
        }
        for transition in transitions
        if transition["step_id"] in _SPEECH_BY_STEP
    ]


@lru_cache(maxsize=8)
def _overlay_font(size: int):
    return ImageFont.truetype(
        "/System/Library/Fonts/Hiragino Sans GB.ttc", size=size
    )


def _draw_status_panel(
    frame: np.ndarray,
    presentation: dict[str, str],
    debug_text: str,
) -> None:
    height, width = frame.shape[:2]
    header_h = max(170, int(height * 0.18))
    footer_h = 36
    split_x = int(width * 0.45)
    cv2.rectangle(frame, (0, 0), (split_x, header_h), (18, 36, 24), -1)
    cv2.rectangle(frame, (split_x, 0), (width, header_h), (28, 28, 28), -1)
    cv2.rectangle(frame, (0, height - footer_h), (width, height), (18, 18, 18), -1)
    cv2.line(frame, (split_x, 14), (split_x, header_h - 14), (70, 70, 70), 2)

    image = Image.fromarray(frame[..., ::-1])
    draw = ImageDraw.Draw(image)
    left_x = 28
    right_x = split_x + 28
    draw.text(
        (left_x, 12), "已识别 / RECOGNIZED",
        font=_overlay_font(24), fill=(74, 235, 130), stroke_width=1,
        stroke_fill=(0, 0, 0),
    )
    draw.text(
        (left_x, 45), presentation["recognized_zh"],
        font=_overlay_font(46), fill=(255, 255, 255), stroke_width=2,
        stroke_fill=(0, 0, 0),
    )
    draw.text(
        (left_x, 108), presentation["recognized_en"],
        font=_overlay_font(28), fill=(215, 235, 220), stroke_width=1,
        stroke_fill=(0, 0, 0),
    )
    draw.text(
        (right_x, 12), "下一步 / NEXT",
        font=_overlay_font(24), fill=(255, 202, 64), stroke_width=1,
        stroke_fill=(0, 0, 0),
    )
    draw.text(
        (right_x, 48), presentation["next_zh"],
        font=_overlay_font(36), fill=(255, 255, 255), stroke_width=2,
        stroke_fill=(0, 0, 0),
    )
    draw.text(
        (right_x, 108), presentation["next_en"],
        font=_overlay_font(23), fill=(255, 225, 145), stroke_width=1,
        stroke_fill=(0, 0, 0),
    )
    draw.text(
        (12, height - 30), debug_text,
        font=_overlay_font(18), fill=(215, 215, 215),
    )
    frame[:] = np.asarray(image)[..., ::-1]


def _draw_annotation_overlay(frame, *, pts_ms, engine, annotations, detections, hands,
                              yolo_ms, recent_events, predicted_step_id):
    h, w = frame.shape[:2]

    # ── detection boxes ──
    for label, conf, box in detections:
        cv2.rectangle(frame, (box[0], box[1]), (box[2], box[3]), (0, 255, 255), 4)
        cv2.putText(frame, f"{label} {conf:.2f}", (box[0], box[1] - 4),
                    _FONT, 0.8, (0, 255, 255), 2)

    # ── hand skeletons ──
    for hand in hands:
        pts = hand.landmarks_px.astype(int)
        for a, b in HAND_EDGES:
            cv2.line(frame, tuple(pts[a]), tuple(pts[b]), HAND_COLOR, 3)
        for p in pts:
            cv2.circle(frame, tuple(p), 4, HAND_COLOR, -1)

    # ── bilingual status panel ──
    step = engine.current_step
    ctx = engine.context
    progress = ctx.step_progress
    next_step_id = None if ctx.step_status == "completed" else step.id
    presentation = _presentation_for(predicted_step_id, next_step_id)
    _draw_status_panel(
        frame,
        presentation,
        (
            f"{pts_ms / 1000.0:.2f}s | YOLO {yolo_ms:.0f}ms | "
            f"detections {len(detections)} | hands {len(hands)} | "
            f"evidence {progress.score:.2f}/{step.completion_policy.threshold:.2f} | "
            f"hits {progress.consecutive_hits}/{step.completion_policy.consecutive_hits}"
        ),
    )

    # ── AI vs Expected ──
    ann = annotations.lookup(pts_ms) if annotations else None
    predicted_id = predicted_step_id
    expected_id = ann.expected_step_id if ann else "UNLABELED"

    if ann is None:
        comparison = "UNLABELED"
        cmp_color = (160, 160, 160)
    elif predicted_id == expected_id:
        comparison = "MATCH"
        cmp_color = (0, 220, 0)
    else:
        comparison = "MISMATCH"
        cmp_color = (0, 0, 255)

    cv2.rectangle(frame, (8, h - 92), (410, h - 44), (24, 24, 24), -1)
    cv2.putText(frame, f"Expected: {expected_id}", (20, h - 60),
                _FONT, 0.65, (225, 225, 225), 2)
    cv2.rectangle(frame, (w - 190, h - 102), (w - 8, h - 44), cmp_color, -1)
    cv2.putText(frame, comparison, (w - 174, h - 64),
                _FONT, 0.8, (255, 255, 255), 2)

    # ── recent events ──
    for i, ev in enumerate(recent_events[-3:]):
        cv2.putText(frame, ev[:60], (8, h - 110 - i * 20),
                    _FONT, 0.35, (160, 200, 255), 1)

    return {
        "predicted_step_id": predicted_id,
        "expected_step_id": expected_id,
        "comparison": comparison.lower(),
    }


def run(args: argparse.Namespace) -> None:
    if args.local_narration and not args.render_video:
        raise ValueError("--local-narration requires --render-video")

    cfg = DomainConfig.load(DOMAIN_PACK)
    recipe = load_recipe("sop/tomato_to_fridge.json")

    run_dir = Path(args.run_dir)
    paths = SessionPaths(root=run_dir)
    paths.root.mkdir(parents=True, exist_ok=True)
    (paths.root / "keyframes").mkdir(exist_ok=True)

    existing_events = paths.root / "events.jsonl"
    if existing_events.exists() and existing_events.stat().st_size > 0:
        raise FileExistsError(f"events.jsonl already exists in {run_dir} — refusing to overwrite")

    device = args.device or cfg.detector_device
    conf = args.conf if args.conf is not None else cfg.detector_conf
    detect_every = args.detect_every or cfg.detect_every
    table_frac = args.table_fraction if args.table_fraction is not None else cfg.table_fraction
    stability = args.stability or cfg.stability_frames

    # ── annotations ──
    ann_timeline: AnnotationTimeline | None = None
    if args.annotations:
        valid_ids = {s.id for s in recipe.steps}
        ann_timeline = load_annotations(args.annotations, valid_step_ids=valid_ids)
        print(f"annotations: {args.annotations} ({len(ann_timeline.segments)} segments)")

    engine = StateEngine(session_id=SESSION_ID, recipe=recipe, started_at=SESSION_EPOCH)
    detector = ObjectDetector(device=device, conf=conf)
    detector.set_vocab(cfg.vocab)
    hand_tracker = HandTracker()
    fusion = InteractionTracker(k_frames=3)
    source = open_source(args.source)
    fps = getattr(source, "fps", 30.0)
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

    writer = None
    if args.render_video:
        writer = AnnotatedVideoWriter(paths.root / "annotated.mp4", fps, (w, h))

    events_log = EventLog(paths.events)
    (paths.root / "events.jsonl").touch()
    obs_path = paths.root / "observations.jsonl"
    snap_path = paths.root / "snapshots.jsonl"
    latency_path = paths.root / "latency.csv"
    comp_path = paths.root / "comparison.jsonl"
    for p in (obs_path, snap_path, latency_path, comp_path):
        p.touch()

    seq = 0
    frame_idx = 0
    total_events = 0
    event_counts: dict[str, int] = {}
    yolo_latencies: list[float] = []
    hand_latencies: list[float] = []
    state_latencies: list[float] = []
    total_latencies: list[float] = []
    labeled_frames = 0
    matched_frames = 0
    mismatched_frames = 0
    predicted_transitions: list[dict] = []
    last_step_id: str | None = None
    recognized_step_id: str | None = None
    last_engine_step: str | None = None

    with latency_path.open("w", newline="") as lf:
        lw = csv.writer(lf)
        lw.writerow(["frame_idx", "inference_ran", "yolo_ms", "hand_ms", "state_update_ms", "total_ms"])

    def emit(envelope):
        nonlocal total_events, recognized_step_id
        t0 = time.monotonic()
        result = engine.consume(envelope)
        recognized_step_id = _recognized_step_id(recognized_step_id, result)
        state_latencies.append((time.monotonic() - t0) * 1000.0)
        total_events += 1
        event_counts[envelope.type] = event_counts.get(envelope.type, 0) + 1
        events_log.append(envelope)
        snap = build_task_snapshot(engine.context, engine.current_step)
        with snap_path.open("a", encoding="utf-8") as f:
            f.write(snap.model_dump_json() + "\n")

    print(f"source={args.source}  device={device}  detect_every={detect_every}")
    print(f"run_dir={run_dir}  render={'on' if writer else 'off'}")
    print(f"Recipe: {recipe.dish} ({len(recipe.steps)} steps)")

    yolo_ms_last = 0.0

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
                yolo_ms_last = yolo_ms

            t_hand = time.monotonic()
            hands = hand_tracker.detect(frame, timestamp_ms=pts_ms)
            hand_ms = (time.monotonic() - t_hand) * 1000.0
            hand_latencies.append(hand_ms)

            hand_data = [(h.handedness,
                          (float(h.palm_center[0]), float(h.palm_center[1])),
                          _to_xyxy(h.box), h.is_gripping) for h in hands]

            can_dets = canonicalize_detections(
                [(d.label, d.conf, _to_xyxy(d.box)) for d in raw_dets]
            )

            emitted_types: list[str] = []
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

            # ── track predicted transitions ──
            cur_step = recognized_step_id or engine.context.current_step_id
            if cur_step != last_step_id:
                predicted_transitions.append({
                    "step_id": cur_step, "first_frame": frame_idx, "pts_ms": round(pts_ms, 2),
                })
                last_step_id = cur_step
                task_tracker.reset_region_events()

            # ── observations ──
            obs_row = {
                "frame_idx": frame_idx, "pts_ms": round(pts_ms, 2),
                "inference_ran": inference_ran,
                "current_step_id": cur_step,
                "next_step_id": engine.context.current_step_id,
                "raw_detections": [
                    {"label": d.label, "conf": round(d.conf, 3), "box": _to_xyxy(d.box)}
                    for d in raw_dets
                ],
                "canonical_detections": [
                    {"label": d[0], "conf": round(d[1], 3), "box": d[2]} for d in can_dets
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
                lw.writerow([frame_idx, int(inference_ran), round(yolo_ms, 2),
                             round(hand_ms, 2),
                             round(state_latencies[-1], 2) if state_latencies else 0,
                             round(total_ms, 2)])

            # ── comparison ──
            cmp = None
            if ann_timeline is not None:
                cmp = _draw_annotation_overlay(
                    frame, pts_ms=pts_ms, engine=engine,
                    annotations=ann_timeline,
                    detections=can_dets if inference_ran else [],
                    hands=hands, yolo_ms=yolo_ms_last,
                    recent_events=[],
                    predicted_step_id=cur_step,
                )
                with comp_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "frame_idx": frame_idx, "pts_ms": round(pts_ms, 2),
                        "predicted_step_id": cmp["predicted_step_id"],
                        "expected_step_id": cmp["expected_step_id"],
                        "comparison": cmp["comparison"],
                    }, ensure_ascii=False) + "\n")
                ann = ann_timeline.lookup(pts_ms)
                if ann is not None:
                    labeled_frames += 1
                    if cmp["predicted_step_id"] == cmp["expected_step_id"]:
                        matched_frames += 1
                    else:
                        mismatched_frames += 1
            elif writer is not None:
                # render-video without annotations: just draw AI state
                cmp = _draw_annotation_overlay(
                    frame, pts_ms=pts_ms, engine=engine,
                    annotations=None,
                    detections=can_dets if inference_ran else [],
                    hands=hands, yolo_ms=yolo_ms_last,
                    recent_events=[],
                    predicted_step_id=cur_step,
                )

            if writer is not None:
                writer.write(frame)

            frame_idx += 1
            if frame_idx % 100 == 0:
                print(f"  frame {frame_idx}  events={total_events}  "
                      f"step={cur_step}  score={engine.context.step_progress.score:.2f}")
            if args.max_frames and frame_idx >= args.max_frames:
                break

            # ── reset tracker on real engine step transition ──
            engine_step = engine.context.current_step_id
            if engine_step != last_engine_step:
                if last_engine_step is not None:
                    task_tracker.reset_region_events()
                last_engine_step = engine_step

    finally:
        source.close()
        hand_tracker.close()
        if writer is not None:
            writer.close()
            print(f"annotated.mp4: {writer.frames_written} frames")

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

    narrated_path = None
    if args.local_narration:
        narration_items = _local_narration_items(predicted_transitions)
        (paths.root / "narration.json").write_text(
            json.dumps(narration_items, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        narrated_path = narrate_run(paths.root, backend="say", voice=args.voice)
        print(f"annotated_narrated.mp4: {narrated_path}")

    ctx = engine.context
    summary = {
        "source": args.source,
        "task_id": cfg.task_id,
        "perception": {
            "detector_device": device, "detector_conf": conf,
            "detect_every": detect_every, "table_fraction": table_frac,
            "stability_frames": stability, "fridge_fallback": list(fridge_fallback),
        },
        "annotated_video": str(paths.root / "annotated.mp4") if writer else None,
        "narrated_video": str(narrated_path) if narrated_path else None,
        "annotations_path": args.annotations,
        "total_frames": frame_idx,
        "inference_count": len(yolo_latencies),
        "total_events": total_events,
        "event_type_counts": event_counts,
        "final_step_id": recognized_step_id or ctx.current_step_id,
        "next_step_id": ctx.current_step_id,
        "step_status": ctx.step_status,
        "final_score": round(ctx.step_progress.score, 3),
        "context_version": ctx.context_version,
        "latency_yolo_mean_ms": round(sum(yolo_latencies) / len(yolo_latencies), 2) if yolo_latencies else 0,
        "latency_yolo_p95_ms": round(_pctl(yolo_latencies, 95), 2),
        "latency_state_mean_ms": round(sum(state_latencies) / len(state_latencies), 2) if state_latencies else 0,
        "latency_state_p95_ms": round(_pctl(state_latencies, 95), 2),
        "latency_total_mean_ms": round(sum(total_latencies) / len(total_latencies), 2) if total_latencies else 0,
        "latency_total_p95_ms": round(_pctl(total_latencies, 95), 2),
        "labeled_frames": labeled_frames,
        "matched_frames": matched_frames,
        "mismatched_frames": mismatched_frames,
        "frame_match_rate": round(matched_frames / labeled_frames, 4) if labeled_frames else None,
        "predicted_transitions": predicted_transitions,
    }
    with (paths.root / "summary.json").open("w") as f:
        json.dump(summary, f, indent=2)

    print(f"\ndone. frames={frame_idx}  events={total_events}")
    print(f"  final step: {ctx.current_step_id}  score: {ctx.step_progress.score:.2f}")
    if labeled_frames:
        print(f"  labeled: {labeled_frames}  match: {matched_frames}  "
              f"mismatch: {mismatched_frames}  rate: {summary['frame_match_rate']}")
    print(f"  transitions: {[t['step_id'] for t in predicted_transitions]}")
    if yolo_latencies:
        print(f"  yolo: mean={summary['latency_yolo_mean_ms']}ms  "
              f"p95={summary['latency_yolo_p95_ms']}ms")


if __name__ == "__main__":
    run(build_parser().parse_args())
