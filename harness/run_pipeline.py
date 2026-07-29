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
from server.gemini_config import gemini_is_configured
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
from server.pipeline.demo_log import DemoLogger, label_zh
from server.pipeline.narrate import (
    complete_item, intro_item, preview_item, question_item, remark_item,
    transition_item,
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
from server.vlm.detection_context import confident_detection_items


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", required=True, help="video file path (not webcam)")
    ap.add_argument("--sop", default="sop/tomato_egg.json")
    ap.add_argument("--device", default="cpu", help="yolo device; cpu for determinism")
    ap.add_argument("--detect-every", type=int, default=3)
    ap.add_argument("--keyframe-interval", type=float, default=3.0, help="seconds")
    ap.add_argument(
        "--vlm-interval",
        type=float,
        default=5.0,
        help="seconds between Gemini visual checks",
    )
    ap.add_argument(
        "--preview-band",
        type=float,
        default=0.85,
        help="announce the next step at this fraction of completion threshold",
    )
    ap.add_argument("--script", default=None, help="scripted evidence JSON")
    ap.add_argument("--run-tag", default=None)
    ap.add_argument("--max-frames", type=int, default=0)
    ap.add_argument("--k-frames", type=int, default=3, help="fusion debounce")
    ap.add_argument("--render", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument(
        "--demo-log",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="show clean presentation log and use curated overlay boxes",
    )
    ap.add_argument(
        "--vlm",
        choices=["auto", "off", "gemini"],
        default="auto",
        help="auto enables Gemini when GEMINI_API_KEY is configured",
    )
    ap.add_argument(
        "--narrate",
        choices=["off", "say", "gemini", "iflytek"],
        default="off",
    )
    ap.add_argument("--voice", default="Tingting", help="say backend voice")
    ap.add_argument(
        "--language",
        default="zh-CN",
        help="iFLYTEK narration target language, for example zh-CN or en-US",
    )
    ap.add_argument(
        "--iflytek-voice",
        default=None,
        help="console-authorized iFLYTEK vcn; defaults to the language env setting",
    )
    ap.add_argument(
        "--iflytek-speed",
        type=int,
        choices=range(101),
        default=50,
        metavar="0-100",
        help="iFLYTEK speaking speed (default: 50)",
    )
    ap.add_argument(
        "--iflytek-volume",
        type=int,
        choices=range(101),
        default=50,
        metavar="0-100",
        help="iFLYTEK volume (default: 50)",
    )
    ap.add_argument(
        "--iflytek-pitch",
        type=int,
        choices=range(101),
        default=50,
        metavar="0-100",
        help="iFLYTEK pitch (default: 50)",
    )
    return ap


def run(args: argparse.Namespace) -> dict:
    if args.detect_every < 1:
        raise SystemExit("--detect-every must be at least 1")
    if args.keyframe_interval <= 0 or args.vlm_interval <= 0:
        raise SystemExit("--keyframe-interval and --vlm-interval must be positive")
    if not 0.5 <= args.preview_band <= 1.0:
        raise SystemExit("--preview-band must be between 0.5 and 1.0")
    if args.narrate == "iflytek":
        from server.iflytek_config import (
            iflytek_credentials,
            iflytek_mt_credentials,
            iflytek_tts_voice,
            normalize_language,
        )
        from server.voice.iflytek_translate import translation_language_code

        try:
            language = normalize_language(args.language)
            iflytek_credentials()
            iflytek_tts_voice(language, args.iflytek_voice)
            if translation_language_code(language) != "cn":
                iflytek_mt_credentials()
        except RuntimeError as exc:
            raise SystemExit(f"iFLYTEK configuration error: {exc}") from None

    video = Path(args.source)
    if not video.is_file():
        raise SystemExit(f"--source must be an existing video file: {video}")
    recipe = load_recipe(args.sop)
    session_language = language if args.narrate == "iflytek" else recipe.language
    session_id = session_id_for(video, recipe.recipe_version_id)
    paths = create_run_dir(session_id, run_tag=args.run_tag)
    print(f"session={session_id}\nrun dir={paths.root}")

    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        raise SystemExit(f"cannot open video {video}")
    fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    frame_ms = 1000.0 / fps
    source_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    source_duration_ms = source_frames * frame_ms

    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    writer = (AnnotatedVideoWriter(paths.annotated, fps=fps,
                                   frame_size=(width, height))
              if args.render else None)
    recent_event_texts: list[str] = []

    engine = StateEngine(
        session_id=session_id,
        recipe=recipe,
        started_at=SESSION_EPOCH,
        user_preferences={"language": session_language, "verbosity": "short"},
    )
    demo = DemoLogger(enabled=args.demo_log)
    first_step = engine.current_step
    demo.step_enter(
        sequence=first_step.sequence,
        total=len(recipe.steps),
        title=first_step.title or first_step.instruction[:12],
    )
    narration: list[dict] = [intro_item(recipe)]
    last_question: str | None = None
    previewed_steps: set[str] = set()
    # Gemini spoken remarks (risk warnings / coach comments): dedupe + pace.
    spoken_remarks: set[str] = set()
    remarks_per_step: dict[str, int] = {}
    last_remark_ms = -1e9
    remark_gap_ms = 20_000.0
    # This is a deterministic gate, not merely prompt wording: a model cannot
    # reveal the next step until engine evidence reaches the near-finish band.
    preview_band = args.preview_band
    log = EventLog(paths.events)
    detector = ObjectDetector(device=args.device, conf=0.10)
    controller = ContextualVocabularyController(detector)
    det_ctx = build_detection_context(engine.context, recipe)
    controller.sync(det_ctx)
    hand_tracker = HandTracker()
    fusion = InteractionTracker(k_frames=args.k_frames)
    confirmer = None
    resolved_vlm = (
        "gemini"
        if args.vlm == "gemini"
        or (args.vlm == "auto" and gemini_is_configured())
        else "off"
    )
    print(
        f"checks: local={args.keyframe_interval:.1f}s, "
        f"gemini={args.vlm_interval:.1f}s "
        f"({resolved_vlm}), "
        f"next-step-band={preview_band:.0%}"
    )
    if resolved_vlm == "gemini":
        from server.pipeline.vlm_hook import VLMConfirmer
        from server.vlm.client import GeminiVLMClient

        confirmer = VLMConfirmer(
            GeminiVLMClient(),
            dish_name=recipe.dish,
            min_gap_ms=args.vlm_interval * 1000.0,
            fast_gap_ms=None,
            periodic=True,
        )
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
        nonlocal seq, det_ctx, last_question
        if envelope.type == "perception.hand_object_relation":
            relation = str(envelope.payload.get("relation") or "")
            handedness = str(envelope.payload.get("hand") or "")
            object_class = str(envelope.payload.get("object_class") or "")
            recent_event_texts.append(
                f"{relation} {handedness}/{object_class}")
            hand_text = "右手" if handedness == "Right" else "左手"
            relation_text = "拿着" if relation == "holding" else "靠近"
            if object_class != "hand":
                demo.signal(f"{hand_text}{relation_text}{label_zh(object_class)}")
        elif envelope.type.startswith("vlm.step_assessment"):
            demo.vlm(
                str(envelope.payload.get("phase") or ""),
                float(envelope.confidence),
                str(envelope.payload.get("reason") or ""),
            )
        log.append(envelope)
        result = engine.consume(envelope)
        seq += 1
        if result.transition is not None:
            recent_event_texts.clear()
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
            next_step = (
                engine.current_step
                if result.transition.next_step_id is not None
                else None
            )
            demo.step_done(next_step.instruction if next_step else None)
            if result.transition.next_step_id is not None:
                narration.append(transition_item(
                    recipe, result.transition.completed_step_id,
                    result.transition.next_step_id, envelope.t_device_ms,
                    include_instruction=(
                        source_duration_ms - envelope.t_device_ms >= 15_000.0
                    ),
                ))
                det_ctx = build_detection_context(engine.context, recipe)
                controller.sync(det_ctx)
                demo.step_enter(
                    sequence=next_step.sequence,
                    total=len(recipe.steps),
                    title=next_step.title or next_step.instruction[:12],
                )
            else:
                narration.append(complete_item(envelope.t_device_ms, recipe))
                demo.dish(recipe.dish)
            last_question = None
        elif result.status == "question_pending" and engine.context.pending_question:
            q = engine.context.pending_question
            if q is not None and q.question != last_question:
                narration.append(question_item(q.question, envelope.t_device_ms))
                last_question = q.question
                print(
                    f"[{envelope.t_device_ms:8.0f}ms] QUESTION: "
                    f"{engine.context.pending_question.question}"
                )
        else:
            # Timing: pre-announce the next step while this one is almost
            # done, so the transition never feels late. Once per step.
            step = engine.current_step
            step_id_now = engine.context.current_step_id
            score = engine.context.step_progress.score
            if (
                step_id_now not in previewed_steps
                and score >= step.completion_policy.threshold * preview_band
            ):
                item = preview_item(recipe, step_id_now, envelope.t_device_ms)
                if item is not None:
                    narration.append(item)
                    print(
                        f"[{envelope.t_device_ms:8.0f}ms] PREVIEW "
                        f"{step_id_now} score={score:.2f} -> hint next step"
                    )
                previewed_steps.add(step_id_now)

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
                if args.demo_log:
                    clean = confident_detection_items(
                        latest_canon,
                        engine.current_step.objects_involved,
                    )
                    demo.detections([d.canonical_label for d in clean])
                    current_step = engine.current_step
                    score = engine.context.step_progress.score
                    threshold = current_step.completion_policy.threshold
                    demo.score(score, threshold, hit=score >= threshold)

            # Gemini has its own fixed 5 s scheduler. Calling the gate every frame
            # keeps a requested 5 s interval from being rounded up to the next
            # 3 s local keyframe (which previously made it 6 s in practice).
            if confirmer is not None:
                # A VLM/network failure must never kill the session loop;
                # the engine simply proceeds without that evidence.
                try:
                    vlm_env = confirmer.maybe_confirm(
                        engine.context,
                        engine.current_step,
                        frame,
                        session_id=session_id,
                        seq=seq,
                        pts_ms=pts_ms,
                        frame_idx=frame_idx,
                        detections=latest_canon,
                        hands=hands,
                        frame_wh=(width, height),
                    )
                except Exception as exc:  # noqa: BLE001
                    vlm_env = None
                    print(f"[{pts_ms:8.0f}ms] VLM ERROR (skipped): {exc}",
                          file=sys.stderr)
                if vlm_env is not None:
                    assessed_step_id = vlm_env.payload.get("step_id")
                    frame_id = str(
                        vlm_env.payload.get("frame_id")
                        or f"frame_{frame_idx:06d}"
                    )
                    # Keep the exact clean frame assessed by Gemini. VLM calls
                    # now have an independent cadence and may not coincide with
                    # a 3 s timeline keyframe.
                    cv2.imwrite(
                        str(paths.keyframes_dir / f"vlm_{frame_id}.jpg"),
                        frame,
                    )
                    print(
                        f"[{pts_ms:8.0f}ms] GEMINI "
                        f"phase={vlm_env.payload.get('phase')} "
                        f"confidence={vlm_env.confidence:.2f} "
                        f"reason={vlm_env.payload.get('reason')}"
                    )
                    # Make Gemini visible on the annotated video too.
                    recent_event_texts.append(
                        f"GEMINI {vlm_env.payload.get('phase')}: "
                        f"{str(vlm_env.payload.get('reason') or '')[:36]}"
                    )
                    emit(vlm_env)
                    step_advanced = (
                        engine.context.current_step_id != assessed_step_id
                    )
                    # Make Gemini audible: risk warnings first, otherwise
                    # its proactive coach_comment. Rate-limited so the
                    # narration never turns into a commentary track.
                    risk = vlm_env.payload.get("risk_level")
                    speak = None
                    if risk in ("warning", "critical"):
                        speak = vlm_env.payload.get("risk_reason")
                        if speak:
                            speak = f"注意，{speak}"
                    if speak is None:
                        speak = vlm_env.payload.get("coach_comment")
                    step_key = engine.context.current_step_id
                    if (
                        not step_advanced
                        and speak
                        and speak not in spoken_remarks
                        and pts_ms - last_remark_ms >= remark_gap_ms
                        and remarks_per_step.get(step_key, 0) < 2
                    ):
                        narration.append(remark_item(speak, pts_ms))
                        spoken_remarks.add(speak)
                        last_remark_ms = pts_ms
                        remarks_per_step[step_key] = (
                            remarks_per_step.get(step_key, 0) + 1
                        )
                        print(f"[{pts_ms:8.0f}ms] REMARK {speak}")
                        demo.remark(speak)

            if writer is not None:
                step = engine.current_step
                overlay_detections = (
                    confident_detection_items(latest_canon, step.objects_involved)
                    if args.demo_log
                    else latest_canon
                )
                draw_overlay(
                    frame,
                    detections=overlay_detections,
                    step_id=engine.context.current_step_id,
                    instruction=step.instruction,
                    score=engine.context.step_progress.score,
                    threshold=step.completion_policy.threshold,
                    pending_question=(engine.context.pending_question.question
                                      if engine.context.pending_question else None),
                    recent_events=recent_event_texts,
                    color_text=(f"color={color_state}" if color_state else None),
                    hands=hands,
                    step_sequence=step.sequence,
                    total_steps=len(recipe.steps),
                    step_title=step.title,
                )
                writer.write(frame)

            frame_idx += 1
            if args.max_frames and frame_idx >= args.max_frames:
                break
    finally:
        capture.release()
        hand_tracker.close()
        if confirmer is not None:
            try:
                confirmer.close()
            except Exception as exc:  # noqa: BLE001
                print(f"VLM CLOSE ERROR (ignored): {exc}", file=sys.stderr)
        writer and writer.close()

    (paths.root / "narration.json").write_text(
        json.dumps(narration, ensure_ascii=False, indent=2), encoding="utf-8")
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
        "source_duration_ms": round(source_duration_ms, 2),
        "vlm_mode": resolved_vlm,
        "narrate_mode": args.narrate,
        "args": {k: v for k, v in vars(args).items()},
    }
    paths.meta.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    from server.pipeline.report import write_report
    print(f"report -> {write_report(paths)}")
    if args.narrate != "off":
        try:
            from server.pipeline.narrate import narrate_run
            narrated = narrate_run(
                paths.root,
                args.narrate,
                args.voice,
                language=args.language,
                iflytek_voice=args.iflytek_voice,
                iflytek_speed=args.iflytek_speed,
                iflytek_volume=args.iflytek_volume,
                iflytek_pitch=args.iflytek_pitch,
            )
            print(f"narrated -> {narrated}")
        except Exception as exc:
            print(f"NARRATE ERROR (video kept, narration skipped): {exc}")
    print(
        f"frames={frame_idx} events={len(log)} "
        f"transitions={len(transitions)} final={meta['final_status']}"
    )
    return meta


def main() -> None:
    run(build_parser().parse_args())


if __name__ == "__main__":
    main()
