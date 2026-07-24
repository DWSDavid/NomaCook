"""Live 3-step demo: external webcam -> perception -> scoring engine -> voice.

Separate from harness/run_pipeline.py (the MP4 offline version). This one reads
a live camera and never writes video/JSONL renders. Same brain (detector /
hands / fusion / Gemini VLM / StateEngine), driven by a FrameSource so the only
difference from offline is where frames come from.

Source:
  --source 0            built-in / external webcam by index (default)
  --source 1            second camera (usually the external USB one)
  --source http://...   ESP32 MJPEG URL (later)
  --source path.mov     a video file (for testing without a camera)

Keys (display window):
  q        quit
  space/d  "this step is done" -> force-advance (demo safety net)

Thresholds are intentionally low (sop/live_demo.json). A single keypress, or a
Gemini "likely_complete", advances a step.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from perception.detector import ObjectDetector
from perception.fusion import InteractionTracker
from perception.hands import HandTracker
from server.engine import StateEngine, load_recipe
from server.events import create_event
from server.gemini_config import gemini_is_configured
from server.live.frame_source import open_source
from server.perception import (
    ContextualVocabularyController,
    build_detection_context,
    canonicalize_detections,
)
from server.perception.context import resolve_concept
from server.pipeline.demo_log import DemoLogger
from server.pipeline.evidence import interaction_event, objects_present_event
from server.pipeline.render import draw_overlay
from server.pipeline.session import SESSION_EPOCH, event_id_for, t_server_for

SESSION_ID = "ses_live_demo"


def speak(text: str, voice: str) -> None:
    """Fire-and-forget local TTS. Instant, offline, never blocks the loop."""
    if voice == "off":
        return
    try:
        subprocess.Popen(["say", "-v", voice, text],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001 - voice is a nicety, never fatal
        pass


def presence_conf(step, latest_canon) -> float | None:
    """Confidence that all of this step's objects are on screen, else None."""
    want = {resolve_concept(o).canonical_label for o in step.objects_involved}
    seen = {d.canonical_label: d.conf for d in latest_canon}
    if want <= set(seen):
        return min(seen[label] for label in want)
    return None


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default="0", help="webcam index / URL / file")
    ap.add_argument("--sop", default="sop/live_demo.json")
    ap.add_argument("--device", default="mps", help="yolo device")
    ap.add_argument("--detect-every", type=int, default=3)
    ap.add_argument("--keyframe-interval", type=float, default=1.5, help="seconds")
    ap.add_argument("--vlm-interval", type=float, default=5.0, help="seconds")
    ap.add_argument("--vlm", choices=["auto", "off"], default="auto")
    ap.add_argument("--voice", default="Tingting", help="macOS say voice, or 'off'")
    ap.add_argument("--no-display", action="store_true")
    ap.add_argument("--max-frames", type=int, default=0, help="stop after N (testing)")
    return ap


def run(args: argparse.Namespace) -> None:
    recipe = load_recipe(args.sop)
    engine = StateEngine(session_id=SESSION_ID, recipe=recipe,
                         started_at=SESSION_EPOCH)
    detector = ObjectDetector(device=args.device, conf=0.10)
    controller = ContextualVocabularyController(detector)
    det_ctx = build_detection_context(engine.context, recipe)
    controller.sync(det_ctx)
    hand_tracker = HandTracker()
    fusion = InteractionTracker(k_frames=3)
    demo = DemoLogger(enabled=True)

    confirmer = None
    use_vlm = args.vlm == "auto" and gemini_is_configured()
    if use_vlm:
        from server.pipeline.vlm_hook import VLMConfirmer
        from server.vlm.client import GeminiVLMClient
        confirmer = VLMConfirmer(GeminiVLMClient(), dish_name=recipe.dish,
                                 min_gap_ms=args.vlm_interval * 1000.0,
                                 fast_gap_ms=None, periodic=True)

    first = engine.current_step
    demo.step_enter(sequence=first.sequence, total=len(recipe.steps),
                    title=first.title or first.instruction[:12])
    speak(f"第一步，{first.instruction}", args.voice)
    print(f"source={args.source}  vlm={'on' if use_vlm else 'off'}  "
          f"(空格/d=这步好了, q=退出)")

    source = open_source(args.source)
    seq = 0
    frame_idx = 0
    latest_canon: list = []
    last_kf = -1e9
    recent: list[str] = []

    def emit(envelope) -> None:
        nonlocal seq, det_ctx
        result = engine.consume(envelope)
        seq += 1
        if result.transition is not None:
            recent.clear()
            done = result.transition.completed_step_id
            nxt_id = result.transition.next_step_id
            steps = {s.id: s for s in recipe.steps}
            demo.step_done(steps[nxt_id].instruction if nxt_id else None)
            if nxt_id is not None:
                nxt = steps[nxt_id]
                demo.step_enter(sequence=nxt.sequence, total=len(recipe.steps),
                                title=nxt.title or nxt.instruction[:12])
                speak(f"{steps[done].completion_message or '好'}下一步，"
                      f"{nxt.instruction}", args.voice)
                det_ctx = build_detection_context(engine.context, recipe)
                controller.sync(det_ctx)
            else:
                demo.dish(recipe.dish)
                speak(f"{recipe.dish}准备好了。妈，我会做饭了。", args.voice)
        elif (result.status == "question_pending"
              and engine.context.pending_question is not None):
            q = engine.context.pending_question.question
            speak(q, args.voice)
            print(f"   ❓ {q}")

    try:
        for pts_ms, frame in source.frames():
            step = engine.current_step
            if frame_idx % args.detect_every == 0:
                latest_canon = canonicalize_detections(detector.detect(frame), det_ctx)
            hands = hand_tracker.detect(frame, timestamp_ms=pts_ms)
            demo.detections([d.canonical_label for d in latest_canon])

            for ev in fusion.update(
                t=pts_ms / 1000.0, frame=frame_idx,
                hands=[(h.handedness, h.palm_center, h.box, h.is_gripping)
                       for h in hands],
                detections=[(d.canonical_label, d.conf, d.box) for d in latest_canon],
            ):
                emit(interaction_event(ev, session_id=SESSION_ID, seq=seq))

            if pts_ms - last_kf >= args.keyframe_interval * 1000.0:
                last_kf = pts_ms
                conf = presence_conf(step, latest_canon)
                if conf is not None:
                    emit(objects_present_event(
                        "ready", conf, session_id=SESSION_ID, seq=seq,
                        step_id=step.id, pts_ms=pts_ms, frame_idx=frame_idx))
                if confirmer is not None:
                    try:
                        vlm_env = confirmer.maybe_confirm(
                            engine.context, engine.current_step, frame,
                            session_id=SESSION_ID, seq=seq, pts_ms=pts_ms,
                            frame_idx=frame_idx)
                    except Exception as exc:  # noqa: BLE001
                        vlm_env = None
                        print(f"VLM skipped: {exc}", file=sys.stderr)
                    if vlm_env is not None:
                        demo.vlm(vlm_env.payload.get("phase"),
                                 vlm_env.confidence or 0.0,
                                 vlm_env.payload.get("reason") or "")
                        emit(vlm_env)
                score = engine.context.step_progress.score
                thr = engine.current_step.completion_policy.threshold
                demo.score(score, thr, hit=score >= thr)

            score = engine.context.step_progress.score
            thr = engine.current_step.completion_policy.threshold
            if not args.no_display:
                draw_overlay(
                    frame,
                    detections=latest_canon,
                    step_id=engine.context.current_step_id,
                    instruction=engine.current_step.instruction,
                    score=score,
                    threshold=thr,
                    pending_question=(engine.context.pending_question.question
                                      if engine.context.pending_question else None),
                    recent_events=recent,
                    color_text=None,
                    hands=hands,
                    step_sequence=engine.current_step.sequence,
                    total_steps=len(recipe.steps),
                    step_title=engine.current_step.title or "")
                cv2.imshow("NomaChef Live", frame)
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    break
                if key in (ord(" "), ord("d")):
                    step = engine.current_step
                    pend = engine.context.pending_question
                    emit(create_event(
                        session_id=SESSION_ID, seq=seq,
                        event_type="voice.user_confirmation",
                        t_device_ms=pts_ms, t_server_est=t_server_for(pts_ms),
                        received_at=t_server_for(pts_ms),
                        source="live_keypress_v1",
                        event_id=event_id_for(SESSION_ID, seq),
                        confidence=0.95,
                        payload={"step_id": step.id, "confirmed": True,
                                 "transcript_event_id": f"key_{frame_idx}",
                                 "question_event_id": (pend.triggered_by_event_id
                                                       if pend else f"key_q_{frame_idx}")}))
                    print("   ✓ 用户确认:这步好了")

            frame_idx += 1
            if args.max_frames and frame_idx >= args.max_frames:
                break
    finally:
        source.close()
        hand_tracker.close()
        if confirmer is not None:
            try:
                confirmer.close()
            except Exception:  # noqa: BLE001
                pass
        if not args.no_display:
            cv2.destroyAllWindows()
    print(f"done. frames={frame_idx}, final step={engine.context.current_step_id}, "
          f"status={engine.context.step_status}")


if __name__ == "__main__":
    run(build_parser().parse_args())
