"""Live tomato-to-fridge demo: camera → perception → state engine → overlay.

Reads perception configuration from domain_packs/kitchen/tomato_to_fridge.yaml.
CLI args override defaults. Step-aware YOLO vocabulary via DomainConfig.

Keys:
  q        quit
  space/d  manual confirm (only when --allow-manual-confirm is set)
  r        reset region tracking
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
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
from server.domain.config import DomainConfig
from server.engine import StateEngine, load_recipe
from server.engine.snapshot import build_task_snapshot
from server.events import create_event
from server.live.frame_source import open_source
from server.pipeline.session import SESSION_EPOCH, event_id_for, t_server_for

SESSION_ID = "ses_tomato_fridge_live"
DOMAIN_PACK = Path(__file__).resolve().parent.parent / "domain_packs" / "kitchen" / "tomato_to_fridge.yaml"


def _parse_roi(spec: str) -> tuple[int, int, int, int]:
    parts = spec.split(",")
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("fridge-roi: x1,y1,x2,y2")
    x1, y1, x2, y2 = map(int, parts)
    if x1 >= x2 or y1 >= y2:
        raise argparse.ArgumentTypeError(f"invalid box order: {x1},{y1},{x2},{y2}")
    return (x1, y1, x2, y2)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default="0", help="webcam index / video file path")
    ap.add_argument("--device", default=None, help="yolo device (default: from domain pack)")
    ap.add_argument("--detect-every", type=int, default=None)
    ap.add_argument("--conf", type=float, default=None)
    ap.add_argument("--table-fraction", type=float, default=None)
    ap.add_argument("--stability", type=int, default=None)
    ap.add_argument("--allow-manual-confirm", action="store_true")
    ap.add_argument("--save-session", action="store_true",
                    help="write events.jsonl + snapshots.jsonl to data/sessions/")
    ap.add_argument("--fridge-roi", type=_parse_roi, default=None)
    ap.add_argument("--no-display", action="store_true")
    ap.add_argument("--max-frames", type=int, default=0)
    return ap


def draw_overlay(frame, *, engine, tomato_box=None, fridge_region=None,
                 table_region=None, detections=0, hands=0, events=None,
                 manual_mode=False):
    h, w = frame.shape[:2]
    if fridge_region is not None:
        cv2.rectangle(frame, (fridge_region[0], fridge_region[1]),
                      (fridge_region[2], fridge_region[3]), (255, 0, 0), 2)
        cv2.putText(frame, "FRIDGE", (fridge_region[0], fridge_region[1] - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 1)
    if table_region is not None:
        cv2.rectangle(frame, (table_region[0], table_region[1]),
                      (table_region[2], table_region[3]), (0, 255, 0), 2)
        cv2.putText(frame, "TABLE", (table_region[0], table_region[3] + 14),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
    if tomato_box is not None:
        cv2.rectangle(frame, (tomato_box[0], tomato_box[1]),
                      (tomato_box[2], tomato_box[3]), (0, 255, 255), 2)
        cv2.putText(frame, "tomato", (tomato_box[0], tomato_box[1] - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
    step = engine.current_step
    ctx = engine.context
    progress = ctx.step_progress
    mode_tag = " [MANUAL]" if manual_mode else ""
    cv2.putText(frame, f"Step {step.sequence}/{len(engine.recipe.steps)}: {step.id}{mode_tag}",
                (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
    cv2.putText(frame, (f"Score: {progress.score:.2f}/{step.completion_policy.threshold:.1f}  "
                        f"Hits: {progress.consecutive_hits}/{step.completion_policy.consecutive_hits}  "
                        f"Groups: {len(progress.matched_source_groups)}/{step.completion_policy.min_source_groups}"),
                (8, 44), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
    cv2.putText(frame, f"Status: {ctx.step_status}  |  det={detections}  hands={hands}",
                (8, 64), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
    if events:
        for i, ev in enumerate(events[-4:]):
            cv2.putText(frame, ev, (w - 320, 24 + i * 18),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, (200, 200, 200), 1)
    if step.instruction:
        cv2.putText(frame, step.instruction[:60], (8, h - 12),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 1)


def run(args: argparse.Namespace) -> None:
    cfg = DomainConfig.load(DOMAIN_PACK)
    recipe = load_recipe("sop/tomato_to_fridge.json")
    engine = StateEngine(session_id=SESSION_ID, recipe=recipe, started_at=SESSION_EPOCH)

    device = args.device or cfg.detector_device
    conf = args.conf if args.conf is not None else cfg.detector_conf
    detect_every = args.detect_every or cfg.detect_every
    table_frac = args.table_fraction if args.table_fraction is not None else cfg.table_fraction
    stability = args.stability or cfg.stability_frames

    detector = ObjectDetector(device=device, conf=conf)
    detector.set_vocab(cfg.vocab_for_step(engine.current_step.id))
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
        table_fraction=table_frac,
        stability_frames=stability,
    )
    if args.fridge_roi is not None:
        task_tracker._fridge_box = args.fridge_roi
    else:
        task_tracker._fridge_fallback = fridge_fallback

    seq = 0
    frame_idx = 0
    recent_events: list[str] = []
    manual_mode = args.allow_manual_confirm

    session_dir = None
    events_path = None
    snapshots_path = None
    if args.save_session:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        session_dir = Path("data/sessions") / f"tomato_fridge_live_{ts}"
        session_dir.mkdir(parents=True, exist_ok=True)
        events_path = session_dir / "events.jsonl"
        snapshots_path = session_dir / "snapshots.jsonl"
        print(f"session_dir={session_dir}")

    def emit(envelope):
        nonlocal seq
        result = engine.consume(envelope)
        seq += 1
        if result.transition is not None:
            t = result.transition
            print(f"  → {t.completed_step_id} → {t.next_step_id}  score={t.score:.2f}")
            task_tracker.reset_region_events()
            detector.set_vocab(cfg.vocab_for_step(engine.current_step.id))
        if result.status not in ("duplicate", "shadow_ignored"):
            recent_events.append(f"#{envelope.seq} {envelope.type}: {result.status}")
            recent_events[:] = recent_events[-20:]
        if events_path is not None:
            events_path.open("a", encoding="utf-8").write(envelope.model_dump_json() + "\n")
        if snapshots_path is not None:
            snap = build_task_snapshot(engine.context, engine.current_step)
            snapshots_path.open("a", encoding="utf-8").write(snap.model_dump_json() + "\n")

    print(f"source={args.source}  device={device}  conf={conf}  detect_every={detect_every}")
    print(f"table_region: below {table_frac:.0%} of frame height")
    print(f"fridge_fallback: {fridge_fallback}")
    print(f"manual_confirm: {'enabled' if manual_mode else 'disabled'}")
    print(f"Recipe: {recipe.dish} ({len(recipe.steps)} steps)")
    print("Keys: q=quit" + ("  space/d=advance" if manual_mode else "") + "  r=reset regions")

    def _hand_data(hands):
        from perception.tomato_to_fridge_events import _box_center as _bc
        return [(h.handedness, (float(h.palm_center[0]), float(h.palm_center[1])),
                 (int(h.box[0]), int(h.box[1]), int(h.box[2]), int(h.box[3])),
                 h.is_gripping) for h in hands]

    try:
        for pts_ms, frame in source.frames():
            step = engine.current_step
            inference_ran = frame_idx % detect_every == 0

            raw_dets = detector.detect(frame) if inference_ran else []
            hands = hand_tracker.detect(frame, timestamp_ms=pts_ms)
            hd = _hand_data(hands) if hands else []

            can_dets = canonicalize_detections(
                [(d.label, d.confidence,
                  (int(d.box[0]), int(d.box[1]), int(d.box[2]), int(d.box[3])))
                 for d in raw_dets]
            )

            if inference_ran:
                fusion_events = fusion.update(
                    t=pts_ms / 1000.0, frame=frame_idx,
                    hands=[(h[0], h[1], h[2], h[3]) for h in hd],
                    detections=[(d[0], d[1], d[2]) for d in can_dets],
                )
                ie_names = [(ev.event, ev.hand, ev.object) for ev in fusion_events]

                task_events = task_tracker.update(
                    t_ms=pts_ms, detections=can_dets, hands=hd,
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

            tomato_box = None
            for d in can_dets if inference_ran else []:
                if d[0] == "tomato":
                    tomato_box = d[2]
                    break

            if not args.no_display:
                draw_overlay(frame, engine=engine, tomato_box=tomato_box,
                             fridge_region=task_tracker.fridge_region,
                             table_region=task_tracker.table_region,
                             detections=len(raw_dets), hands=len(hands),
                             events=recent_events, manual_mode=manual_mode)
                cv2.imshow("NomaChef — Tomato to Fridge", frame)
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    break
                if manual_mode and key in (ord(" "), ord("d")):
                    pend = engine.context.pending_question
                    emit(create_event(
                        session_id=SESSION_ID, seq=seq,
                        event_type="voice.user_confirmation",
                        t_device_ms=pts_ms, t_server_est=t_server_for(pts_ms),
                        received_at=t_server_for(pts_ms),
                        source="live_keypress_v1",
                        event_id=event_id_for(SESSION_ID, seq),
                        confidence=0.95,
                        payload={
                            "step_id": engine.current_step.id, "confirmed": True,
                            "transcript_event_id": f"key_{frame_idx}",
                            "question_event_id": (
                                pend.triggered_by_event_id if pend else f"key_q_{frame_idx}"
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

    if session_dir is not None:
        summary = {
            "session_id": SESSION_ID, "recipe": recipe.dish,
            "total_frames": frame_idx, "total_events": seq,
            "final_step_id": engine.context.current_step_id,
            "step_status": engine.context.step_status,
            "final_score": engine.context.step_progress.score,
            "context_version": engine.context.context_version,
        }
        json.dump(summary, (session_dir / "summary.json").open("w"), indent=2)
        print(f"session saved to {session_dir}")

    print(f"done. frames={frame_idx}, final step={engine.context.current_step_id}, "
          f"status={engine.context.step_status}")


if __name__ == "__main__":
    run(build_parser().parse_args())
