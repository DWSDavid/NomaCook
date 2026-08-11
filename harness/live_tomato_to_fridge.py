"""Live tomato-to-fridge demo: camera -> perception -> state engine -> bilingual overlay -> session recording.

Usage:
  --source 0              built-in webcam (default)
  --source path.mov       video file for testing
  --save-session          write evidence + snapshots + summary
  --session-dir PATH      explicit session directory
  --record-video          save annotated_live.mp4
  --stop-on-complete      auto-exit 2s after task finishes
  --no-display            headless mode (for batch testing)
  --max-frames N          stop after N frames (smoke test)
  --qwen-realtime         enable Qwen realtime voice interaction
  --qwen-model MODEL      override model (default: qwen3.5-omni-flash-realtime)
  --qwen-voice VOICE      override voice (default: Tina)
  --qwen-url URL          override WebSocket URL

Reads perception config from domain_packs/kitchen/tomato_to_fridge.yaml.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

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
from server.engine.hot_memory import HotMemory
from server.engine.snapshot import build_task_snapshot
from server.events import create_event
from server.live.frame_source import open_source
from server.pipeline.render import (
    AnnotatedVideoWriter,
    HAND_COLOR,
    HAND_EDGES,
    _CJK_FONT_CANDIDATES,
)
from server.pipeline.session import SESSION_EPOCH, event_id_for, t_server_for

SESSION_ID = "ses_tomato_fridge_live"
DOMAIN_PACK = Path(__file__).resolve().parent.parent / "domain_packs" / "kitchen" / "tomato_to_fridge.yaml"
_FONT = cv2.FONT_HERSHEY_SIMPLEX

# CJK font support
try:
    from PIL import Image, ImageDraw, ImageFont
    _CJK_PATH = next((p for p in _CJK_FONT_CANDIDATES if Path(p).exists()), None)
    _CJK_FONT = ImageFont.truetype(_CJK_PATH, 22) if _CJK_PATH else None
except Exception:
    _CJK_FONT = None


def _cjk_text(frame, text, xy, color=(255, 255, 255)):
    if _CJK_FONT:
        img = Image.fromarray(frame[..., ::-1])
        ImageDraw.Draw(img).text(xy, text, font=_CJK_FONT, fill=color)
        frame[:] = np.asarray(img)[..., ::-1]
    else:
        cv2.putText(frame, text.encode("ascii", "replace").decode(), xy, _FONT, 0.55, color, 1)


def _parse_roi(spec: str) -> tuple[int, int, int, int]:
    parts = spec.split(",")
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("fridge-roi: x1,y1,x2,y2")
    x1, y1, x2, y2 = map(int, parts)
    if x1 >= x2 or y1 >= y2:
        raise argparse.ArgumentTypeError(f"invalid box: {x1},{y1},{x2},{y2}")
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
                    help="write events, snapshots, observations, latency, summary")
    ap.add_argument("--session-dir", default=None,
                    help="explicit session directory (auto-generated if not set)")
    ap.add_argument("--record-video", action="store_true",
                    help="save annotated_live.mp4")
    ap.add_argument("--stop-on-complete", action="store_true",
                    help="auto-exit after task completes (+ hold)")
    ap.add_argument("--completion-hold-seconds", type=float, default=2.0,
                    help="hold display after completion before exit")
    ap.add_argument("--fridge-roi", type=_parse_roi, default=None)
    ap.add_argument("--no-display", action="store_true")
    ap.add_argument("--max-frames", type=int, default=0)
    ap.add_argument("--qwen-realtime", action="store_true",
                    help="enable Qwen realtime voice interaction")
    ap.add_argument("--qwen-model", default="qwen3.5-omni-flash-realtime",
                    help="Qwen model (default: qwen3.5-omni-flash-realtime)")
    ap.add_argument("--qwen-voice", default="Tina", help="Qwen voice (default: Tina)")
    ap.add_argument("--qwen-url", default=None, help="override Qwen WebSocket URL")
    return ap


def _to_xyxy(box):
    return (int(box[0]), int(box[1]), int(box[2]), int(box[3]))


def run(args: argparse.Namespace) -> None:
    cfg = DomainConfig.load(DOMAIN_PACK)
    recipe = load_recipe("sop/tomato_to_fridge.json")
    engine = StateEngine(session_id=SESSION_ID, recipe=recipe, started_at=SESSION_EPOCH)

    device = args.device or cfg.detector_device
    conf = args.conf if args.conf is not None else cfg.detector_conf
    detect_every = args.detect_every or cfg.detect_every
    table_frac = args.table_fraction if args.table_fraction is not None else cfg.table_fraction
    stability = args.stability or cfg.stability_frames

    hot = HotMemory()

    # ── qwen env check ──
    if args.qwen_realtime:
        from server.voice.qwen_realtime import QwenRealtimeAdapter
        try:
            qwen = QwenRealtimeAdapter(
                model=args.qwen_model,
                voice=args.qwen_voice,
                url_override=args.qwen_url,
                hot_memory=hot,
            )
        except RuntimeError as exc:
            print(f"[qwen] {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        qwen = None

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
    if args.fridge_roi is not None:
        task_tracker._fridge_box = args.fridge_roi
    else:
        task_tracker._fridge_fallback = fridge_fallback

    # ── session directory ──
    if args.session_dir:
        session_dir = Path(args.session_dir)
        if session_dir.exists() and list(session_dir.glob("*")):
            raise FileExistsError(f"session dir exists and is non-empty: {session_dir}")
        session_dir.mkdir(parents=True, exist_ok=True)
    elif args.save_session or args.record_video:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        session_dir = Path("data/sessions") / f"tomato_fridge_live_{ts}"
        session_dir.mkdir(parents=True, exist_ok=True)
    else:
        session_dir = None

    # ── qwen with session dir ──
    if qwen is not None and session_dir:
        qwen._session_dir = session_dir
        (session_dir / "realtime_events.jsonl").touch()

    events_path = session_dir / "events.jsonl" if session_dir else None
    snaps_path = session_dir / "snapshots.jsonl" if session_dir else None
    obs_path = session_dir / "observations.jsonl" if session_dir else None
    latency_path = session_dir / "latency.csv" if session_dir else None
    summary_path = session_dir / "summary.json" if session_dir else None

    if session_dir:
        for p in (events_path, snaps_path, obs_path, latency_path):
            p.touch()

    writer = None
    if args.record_video and session_dir:
        writer = AnnotatedVideoWriter(session_dir / "annotated_live.mp4", fps, (w, h))

    seq = 0
    frame_idx = 0
    total_events = 0
    event_counts: dict[str, int] = {}
    yolo_lats: list[float] = []
    hand_lats: list[float] = []
    state_lats: list[float] = []
    total_lats: list[float] = []
    last_step_id: str | None = None
    predicted_transitions: list[dict] = []
    exit_reason: str = "source_ended"
    completed_at_frame: int | None = None
    started_at = time.monotonic()

    if latency_path:
        with latency_path.open("w", newline="") as lf:
            csv.writer(lf).writerow(["frame_idx", "inference_ran", "yolo_ms", "hand_ms", "state_update_ms", "total_ms"])

    def emit(envelope):
        nonlocal total_events
        t0 = time.monotonic()
        result = engine.consume(envelope)
        state_lats.append((time.monotonic() - t0) * 1000.0)
        total_events += 1
        et = envelope.type
        event_counts[et] = event_counts.get(et, 0) + 1

        if events_path:
            with events_path.open("a", encoding="utf-8") as f:
                f.write(envelope.model_dump_json() + "\n")
        if snaps_path:
            snap = build_task_snapshot(engine.context, engine.current_step)
            with snaps_path.open("a", encoding="utf-8") as f:
                f.write(snap.model_dump_json() + "\n")

        # ── hot memory ──
        recent_event = {"type": envelope.type, "seq": envelope.seq, "confidence": envelope.confidence}
        transition_info = None
        if result.transition is not None:
            transition_info = {
                "from_step": result.transition.completed_step_id,
                "to_step": result.transition.next_step_id,
                "score": result.transition.score,
                "decision_id": result.transition.decision_id,
            }
        hot.update(
            snapshot=build_task_snapshot(engine.context, engine.current_step),
            recent_events=[recent_event],
            latest_transition=transition_info,
        )
        if session_dir:
            hot.write_latest_snapshot(session_dir)

    print(f"source={args.source}  device={device}  detect_every={detect_every}")
    print(f"fridge_fb={fridge_fallback}")
    print(f"session={'on' if session_dir else 'off'}  video={'on' if writer else 'off'}  "
          f"stop_on_complete={'on' if args.stop_on_complete else 'off'}")
    print(f"Recipe: {recipe.dish} ({len(recipe.steps)} steps)")

    # ── start Qwen background thread ──
    qwen_thread: threading.Thread | None = None
    if qwen is not None:
        print(f"qwen-realtime: model={args.qwen_model} voice={args.qwen_voice}")
        def _qwen_runner():
            asyncio.run(qwen.run())
        qwen_thread = threading.Thread(target=_qwen_runner, daemon=True, name="qwen-realtime")
        qwen_thread.start()

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
                yolo_lats.append(yolo_ms)

            t_hand = time.monotonic()
            hands = hand_tracker.detect(frame, timestamp_ms=pts_ms)
            hand_ms = (time.monotonic() - t_hand) * 1000.0
            hand_lats.append(hand_ms)

            hd = [(h.handedness, (float(h.palm_center[0]), float(h.palm_center[1])),
                   _to_xyxy(h.box), h.is_gripping) for h in hands]

            can_dets = canonicalize_detections(
                [(d.label, d.conf, _to_xyxy(d.box)) for d in raw_dets]
            )

            emitted_types: list[str] = []
            if inference_ran:
                fusion_events = fusion.update(
                    t=pts_ms / 1000.0, frame=frame_idx,
                    hands=[(h.handedness, (float(h.palm_center[0]), float(h.palm_center[1])),
                            _to_xyxy(h.box), h.is_gripping) for h in hands],
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
                    seq += 1
                    emitted_types.append(tev.event_type)

            cur_step = engine.context.current_step_id
            if cur_step != last_step_id:
                predicted_transitions.append({
                    "step_id": cur_step, "first_frame": frame_idx, "pts_ms": round(pts_ms, 2),
                })
                last_step_id = cur_step
                task_tracker.reset_region_events()

            # ── observations ──
            if obs_path:
                with obs_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "frame_idx": frame_idx, "pts_ms": round(pts_ms, 2),
                        "inference_ran": inference_ran,
                        "current_step_id": cur_step,
                        "raw_detections": [
                            {"label": d.label, "conf": round(d.conf, 3), "box": _to_xyxy(d.box)}
                            for d in raw_dets
                        ],
                        "canonical_detections": [
                            {"label": d[0], "conf": round(d[1], 3), "box": d[2]}
                            for d in can_dets
                        ],
                        "hand_count": len(hands),
                        "hands_gripping": sum(1 for h in hands if h.is_gripping),
                        "emitted_event_types": emitted_types,
                    }, ensure_ascii=False) + "\n")

            total_ms = (time.monotonic() - t0) * 1000.0
            total_lats.append(total_ms)
            if latency_path:
                with latency_path.open("a", newline="") as lf:
                    csv.writer(lf).writerow([
                        frame_idx, int(inference_ran), round(yolo_ms, 2),
                        round(hand_ms, 2),
                        round(state_lats[-1], 2) if state_lats else 0,
                        round(total_ms, 2),
                    ])

            ctx = engine.context
            step = engine.current_step

            # ── bilingual overlay ──
            if writer is not None or not args.no_display:
                hh, ww = frame.shape[:2]
                progress = ctx.step_progress
                thr = step.completion_policy.threshold
                score_pct = min(progress.score / max(thr, 1e-6), 1.0)
                completed_now = ctx.step_status == "completed"

                # boxes
                for label, conf, box in can_dets if inference_ran else []:
                    cv2.rectangle(frame, (box[0], box[1]), (box[2], box[3]),
                                  (0, 255, 255) if label == "tomato" else (255, 160, 0), 1)
                    cv2.putText(frame, f"{label} {conf:.2f}", (box[0], box[1] - 4),
                                _FONT, 0.35, (0, 255, 255), 1)
                for hand in hands:
                    pts = hand.landmarks_px.astype(int)
                    for a, b in HAND_EDGES[:5]:
                        cv2.line(frame, tuple(pts[a]), tuple(pts[b]), HAND_COLOR, 1)
                    cv2.circle(frame, tuple(pts[0]), 3, HAND_COLOR, -1)

                # score bar
                cv2.rectangle(frame, (0, 0), (ww, 90), (24, 24, 24), -1)
                bar_w = int((ww - 16) * score_pct)
                cv2.rectangle(frame, (8, 80), (8 + bar_w, 86), (0, 220, 0), -1)
                cv2.rectangle(frame, (8, 80), (ww - 8, 86), (90, 90, 90), 1)

                # left: recognized
                _cjk_text(frame, "已识别", (8, 6), (180, 180, 180))
                _cjk_text(frame, f"Step {step.sequence}/{len(recipe.steps)}: {step.id}",
                          (8, 30), (255, 255, 255))
                _cjk_text(frame, f"Score {progress.score:.2f}/{thr:.1f}  "
                          f"Hits {progress.consecutive_hits}/{step.completion_policy.consecutive_hits}  "
                          f"Src {len(progress.matched_source_groups)}/{step.completion_policy.min_source_groups}",
                          (8, 54), (160, 200, 160))

                # right: next / complete
                if completed_now:
                    _cjk_text(frame, "任务完成", (ww - 200, 6), (0, 255, 0))
                    _cjk_text(frame, "Task complete", (ww - 200, 30), (0, 255, 0))
                else:
                    _cjk_text(frame, "下一步", (ww - 120, 6), (180, 180, 180))
                    next_step = recipe.steps[step.sequence] if step.sequence < len(recipe.steps) else None
                    if next_step:
                        _cjk_text(frame, next_step.id[:30], (ww - 200, 30), (255, 255, 255))

                # bottom: latency + fps
                cv2.putText(frame, f"{pts_ms / 1000.0:.2f}s  YOLO {yolo_ms:.0f}ms  "
                            f"total {total_ms:.0f}ms  det={len(raw_dets)} h={len(hands)}",
                            (8, hh - 12), _FONT, 0.4, (160, 160, 160), 1)

            if writer is not None:
                writer.write(frame)

            if not args.no_display:
                cv2.imshow("NomaChef — Tomato to Fridge", frame)
                key = cv2.waitKey(1) & 0xFF
                if key in (ord("q"), 27):  # q or ESC
                    exit_reason = "user_quit"
                    break

            frame_idx += 1

            # ── stop on complete ──
            if args.stop_on_complete and ctx.step_status == "completed":
                if completed_at_frame is None:
                    completed_at_frame = frame_idx
                    print(f"  ✓ task completed at frame {frame_idx}")
                if frame_idx - completed_at_frame >= args.completion_hold_seconds * fps:
                    exit_reason = "completed"
                    break

            if args.max_frames and frame_idx >= args.max_frames:
                exit_reason = "max_frames"
                break

    except KeyboardInterrupt:
        exit_reason = "interrupted"
    except Exception as exc:
        exit_reason = f"error: {exc}"
        print(f"  ! error: {exc}", file=sys.stderr)
    finally:
        # ── stop Qwen ──
        if qwen is not None:
            qwen.request_stop()
        source.close()
        hand_tracker.close()
        if writer is not None:
            writer.close()
            print(f"annotated_live.mp4: {writer.frames_written} frames")
        if not args.no_display:
            cv2.destroyAllWindows()

    ended_at = time.monotonic()
    ctx = engine.context

    # ── summary ──
    def _pctl(vals, pct):
        if not vals: return 0
        s = sorted(vals)
        k = (len(s) - 1) * pct / 100.0
        f, c = math.floor(k), math.ceil(k)
        return s[int(k)] if f == c else s[f] * (c - k) + s[c] * (k - f)

    summary = {
        "session_id": SESSION_ID, "source": args.source,
        "task_id": cfg.task_id, "started_at": started_at, "ended_at": ended_at,
        "total_frames": frame_idx, "total_events": total_events,
        "event_type_counts": event_counts,
        "final_step_id": ctx.current_step_id, "step_status": ctx.step_status,
        "final_score": round(ctx.step_progress.score, 3),
        "context_version": ctx.context_version,
        "predicted_transitions": predicted_transitions,
        "latency_yolo_mean_ms": round(sum(yolo_lats) / len(yolo_lats), 2) if yolo_lats else 0,
        "latency_yolo_p95_ms": round(_pctl(yolo_lats, 95), 2),
        "latency_hand_mean_ms": round(sum(hand_lats) / len(hand_lats), 2) if hand_lats else 0,
        "latency_hand_p95_ms": round(_pctl(hand_lats, 95), 2),
        "latency_state_mean_ms": round(sum(state_lats) / len(state_lats), 2) if state_lats else 0,
        "latency_state_p95_ms": round(_pctl(state_lats, 95), 2),
        "latency_total_mean_ms": round(sum(total_lats) / len(total_lats), 2) if total_lats else 0,
        "latency_total_p95_ms": round(_pctl(total_lats, 95), 2),
        "effective_fps": round(frame_idx / (ended_at - started_at), 2) if ended_at > started_at else 0,
        "exit_reason": exit_reason,
        "annotated_video": str(session_dir / "annotated_live.mp4") if writer else None,
    }

    if qwen is not None:
        qs = qwen.stats()
        summary["qwen_enabled"] = True
        summary["qwen_connected"] = qwen.connected
        summary["qwen_model"] = args.qwen_model
        summary["qwen_voice"] = args.qwen_voice
        summary["qwen_user_turn_count"] = qs["user_turn_count"]
        summary["qwen_assistant_turn_count"] = qs["assistant_turn_count"]
        summary["qwen_first_audio_mean_ms"] = round(qs["qwen_first_audio_mean_ms"], 2)
        summary["qwen_first_audio_p95_ms"] = round(qs["qwen_first_audio_p95_ms"], 2)
        summary["qwen_error_count"] = qs["qwen_error_count"]

    if summary_path:
        json.dump(summary, summary_path.open("w"), indent=2)

    print(f"\ndone. frames={frame_idx}  events={total_events}  exit={exit_reason}")
    print(f"  final step: {ctx.current_step_id}  status: {ctx.step_status}  score: {ctx.step_progress.score:.2f}")
    if yolo_lats:
        print(f"  yolo: mean={summary['latency_yolo_mean_ms']}ms  p95={summary['latency_yolo_p95_ms']}ms")
    print(f"  total: mean={summary['latency_total_mean_ms']}ms  p95={summary['latency_total_p95_ms']}ms  "
          f"fps={summary['effective_fps']}")
    print(f"  transitions: {[t['step_id'] for t in predicted_transitions]}")


if __name__ == "__main__":
    run(build_parser().parse_args())
