"""Live tomato-to-fridge demo: camera → perception → state engine → overlay.

Source:
  --source 0            built-in / external webcam (default)
  --source 1            second camera (USB)
  --source path.mov     a video file (for testing without a camera)

Keys:
  q        quit
  space/d  manual confirm (only when --allow-manual-confirm is set)
  r        reset region tracking (re-fetch table/fridge zones)

Runs the full tomato-to-fridge 8-step task graph with StateEngine.
No training, no VLM, no voice — just CV geometry signals feeding the engine.
"""

from __future__ import annotations

import argparse
import sys
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
from server.events import create_event
from server.live.frame_source import open_source
from server.pipeline.session import SESSION_EPOCH, event_id_for, t_server_for

SESSION_ID = "ses_tomato_fridge_live"

TOMATO_FRIDGE_VOCAB = [
    "tomato", "cherry tomato", "red fruit",
    "refrigerator", "fridge", "freezer",
    "table", "kitchen counter", "desk",
    "hand", "person hand",
]


def _parse_roi(spec: str) -> tuple[int, int, int, int]:
    parts = spec.split(",")
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("fridge-roi: x1,y1,x2,y2")
    x1, y1, x2, y2 = map(int, parts)
    if x1 >= x2 or y1 >= y2:
        raise argparse.ArgumentTypeError(f"fridge-roi: x1<{x1}<x2<{x2}> or y1<{y1}<y2<{y2}>")
    return (x1, y1, x2, y2)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default="0", help="webcam index / video file path")
    ap.add_argument("--device", default="mps", help="yolo device")
    ap.add_argument("--detect-every", type=int, default=3)
    ap.add_argument("--conf", type=float, default=0.10, help="YOLO confidence threshold")
    ap.add_argument("--table-fraction", type=float, default=0.70,
                    help="table region: pixels below this fraction of frame height")
    ap.add_argument("--stability", type=int, default=3,
                    help="frames for stable-in-region confirmation")
    ap.add_argument("--allow-manual-confirm", action="store_true",
                    help="enable space/d key to force-advance a step")
    ap.add_argument("--fridge-roi", type=_parse_roi, default=None,
                    help="fridge interior: x1,y1,x2,y2 (overrides auto-detection)")
    ap.add_argument("--no-display", action="store_true")
    ap.add_argument("--max-frames", type=int, default=0)
    return ap


def draw_overlay(
    frame,
    *,
    engine: StateEngine,
    tomato_box=None,
    fridge_region=None,
    table_region=None,
    detections: int = 0,
    hands: int = 0,
    events: list[str] | None = None,
    manual_mode: bool = False,
) -> None:
    h, w = frame.shape[:2]

    if fridge_region is not None:
        fx1, fy1, fx2, fy2 = fridge_region
        cv2.rectangle(frame, (fx1, fy1), (fx2, fy2), (255, 0, 0), 2)
        cv2.putText(frame, "FRIDGE", (fx1, fy1 - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 1)
    if table_region is not None:
        tx1, ty1, tx2, ty2 = table_region
        cv2.rectangle(frame, (tx1, ty1), (tx2, ty2), (0, 255, 0), 2)
        cv2.putText(frame, "TABLE", (tx1, ty2 + 14),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)

    if tomato_box is not None:
        tx1, ty1, tx2, ty2 = tomato_box
        cv2.rectangle(frame, (tx1, ty1), (tx2, ty2), (0, 255, 255), 2)
        cv2.putText(frame, "tomato", (tx1, ty1 - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)

    step = engine.current_step
    ctx = engine.context
    progress = ctx.step_progress
    mode_tag = " [MANUAL]" if manual_mode else ""
    lines = [
        f"Step {step.sequence}/{len(engine.recipe.steps)}: {step.id}{mode_tag}",
        f"Score: {progress.score:.2f}/{step.completion_policy.threshold:.1f}  "
        f"Hits: {progress.consecutive_hits}/{step.completion_policy.consecutive_hits}  "
        f"Groups: {len(progress.matched_source_groups)}/{step.completion_policy.min_source_groups}",
        f"Status: {ctx.step_status}  |  det={detections}  hands={hands}",
    ]
    y = 24
    for line in lines:
        cv2.putText(frame, line, (8, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        y += 20

    if events:
        for i, ev in enumerate(events[-4:]):
            cv2.putText(frame, ev, (w - 320, 24 + i * 18),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, (200, 200, 200), 1)

    if step.instruction:
        cv2.putText(frame, step.instruction[:60], (8, h - 12),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 1)


def _to_xyxy(box) -> tuple[int, int, int, int]:
    return (int(box[0]), int(box[1]), int(box[2]), int(box[3]))


def _palm(p) -> tuple[float, float]:
    return (float(p[0]), float(p[1]))


def run(args: argparse.Namespace) -> None:
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
    if args.fridge_roi is not None:
        task_tracker._fridge_box = args.fridge_roi

    seq = 0
    frame_idx = 0
    recent_events: list[str] = []
    manual_mode = args.allow_manual_confirm

    def emit(envelope) -> None:
        nonlocal seq
        result = engine.consume(envelope)
        seq += 1
        status = result.status
        if result.transition is not None:
            t = result.transition
            print(f"  → {t.completed_step_id} → {t.next_step_id}  score={t.score:.2f}")
            task_tracker.reset_region_events()
        if status not in ("duplicate", "shadow_ignored"):
            recent_events.append(
                f"#{envelope.seq} {envelope.type}: {status}"
            )
            recent_events[:] = recent_events[-20:]

    print(f"source={args.source}  device={args.device}  conf={args.conf}  "
          f"detect_every={args.detect_every}")
    print(f"table_region: below {args.table_fraction:.0%} of frame height")
    print(f"fridge_roi: {args.fridge_roi or 'auto-detect from YOLO + fallback'}")
    print(f"manual_confirm: {'enabled' if manual_mode else 'disabled'}")
    print(f"Recipe: {recipe.dish} ({len(recipe.steps)} steps)")
    print("Keys: q=quit" + ("  space/d=advance" if manual_mode else "") + "  r=reset regions")

    try:
        for pts_ms, frame in source.frames():
            step = engine.current_step
            inference_ran = frame_idx % args.detect_every == 0

            raw_dets = detector.detect(frame) if inference_ran else []
            hands = hand_tracker.detect(frame, timestamp_ms=pts_ms)
            hand_data = [(h.handedness, _palm(h.palm_center), _to_xyxy(h.box), h.is_gripping)
                         for h in hands]

            can_dets = canonicalize_detections(
                [(d.label, d.confidence, _to_xyxy(d.box)) for d in raw_dets]
            )

            # ── trackers only updated on inference frames ──
            if inference_ran:
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

            # ── overlay ──
            tomato_box = None
            for d in can_dets if inference_ran else []:
                if d[0] == "tomato":
                    tomato_box = d[2]
                    break

            if not args.no_display:
                draw_overlay(
                    frame,
                    engine=engine,
                    tomato_box=tomato_box,
                    fridge_region=task_tracker.fridge_region,
                    table_region=task_tracker.table_region,
                    detections=len(raw_dets),
                    hands=len(hands),
                    events=recent_events,
                    manual_mode=manual_mode,
                )
                cv2.imshow("NomaChef — Tomato to Fridge", frame)
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    break
                if manual_mode and key in (ord(" "), ord("d")):
                    pend = engine.context.pending_question
                    current_step = engine.current_step
                    emit(create_event(
                        session_id=SESSION_ID, seq=seq,
                        event_type="voice.user_confirmation",
                        t_device_ms=pts_ms, t_server_est=t_server_for(pts_ms),
                        received_at=t_server_for(pts_ms),
                        source="live_keypress_v1",
                        event_id=event_id_for(SESSION_ID, seq),
                        confidence=0.95,
                        payload={
                            "step_id": current_step.id, "confirmed": True,
                            "transcript_event_id": f"key_{frame_idx}",
                            "question_event_id": (
                                pend.triggered_by_event_id
                                if pend else f"key_q_{frame_idx}"
                            ),
                        },
                        context_version=engine.context.context_version,
                    ))
                    print("   ✓ 用户确认")
                if key == ord("r"):
                    task_tracker.reset_region_events()
                    print("   ↺ regions reset")

            frame_idx += 1
            if args.max_frames and frame_idx >= args.max_frames:
                break

    finally:
        source.close()
        hand_tracker.close()
        cv2.destroyAllWindows()

    print(f"done. frames={frame_idx}, final step={engine.context.current_step_id}, "
          f"status={engine.context.step_status}")


if __name__ == "__main__":
    run(build_parser().parse_args())
