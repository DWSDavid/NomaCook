"""Live preview for SOP-step-aware YOLO-World detection."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import sys
import time

import cv2

from perception.detector import ObjectDetector
from server.engine import load_recipe
from server.engine.models import SessionContext
from server.perception import (
    ContextDetection,
    ContextualVocabularyController,
    build_detection_context,
    canonicalize_detections,
    extract_tomato_egg_color_signals,
)


COLORS = {
    "primary": (0, 220, 0),
    "anchor": (255, 160, 0),
    "confuser": (0, 210, 255),
}


def _source(value: str) -> int | str:
    return int(value) if value.isdigit() else value


def _open_capture(source: int | str) -> cv2.VideoCapture:
    backend = (
        cv2.CAP_AVFOUNDATION
        if sys.platform == "darwin" and isinstance(source, int)
        else cv2.CAP_ANY
    )
    capture = cv2.VideoCapture(source, backend)
    if not capture.isOpened():
        capture.release()
        raise RuntimeError(f"cannot open camera/video source {source!r}")
    return capture


def _draw(
    frame,
    detections: list[ContextDetection],
    latency_ms: float,
    color_text: str | None,
) -> None:
    for detection in detections:
        x1, y1, x2, y2 = detection.box
        color = COLORS[detection.role]
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
        label = (
            f"{detection.canonical_label} {detection.conf:.2f}"
            f" [{detection.role}]"
        )
        cv2.putText(
            frame,
            label,
            (x1, max(20, y1 - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            color,
            2,
            cv2.LINE_AA,
        )
    cv2.putText(
        frame,
        f"YOLO {latency_ms:.0f} ms | q to stop",
        (20, 35),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    if color_text:
        cv2.putText(
            frame,
            color_text,
            (20, 65),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (0, 255, 255),
            2,
            cv2.LINE_AA,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sop", default="sop/tomato_egg.json")
    parser.add_argument("--step", default="step_01_prepare")
    parser.add_argument("--source", default="1")
    parser.add_argument("--device", default="mps")
    parser.add_argument(
        "--raw-conf",
        type=float,
        default=0.10,
        help="model prefilter; per-concept thresholds are applied afterward",
    )
    parser.add_argument("--detect-every", type=int, default=3)
    parser.add_argument("--duration", type=float, default=0)
    parser.add_argument("--no-display", action="store_true")
    parser.add_argument(
        "--color-signals",
        action="store_true",
        help="print tomato/egg HSV signals inside the detected wok ROI",
    )
    parser.add_argument("--list-steps", action="store_true")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    recipe = load_recipe(args.sop)
    if args.list_steps:
        for step in recipe.steps:
            print(f"{step.id}: {step.instruction}")
        return
    if args.no_display and args.duration <= 0:
        parser.error("--no-display requires a positive --duration")
    if args.detect_every < 1:
        parser.error("--detect-every must be at least 1")
    if not 0.0 <= args.raw_conf <= 1.0:
        parser.error("--raw-conf must be between 0 and 1")

    try:
        step = next(step for step in recipe.steps if step.id == args.step)
    except StopIteration:
        parser.error(f"unknown step {args.step!r}; use --list-steps")

    session_context = SessionContext(
        session_id="ses_context_demo",
        recipe_version_id=recipe.recipe_version_id,
        current_step_id=step.id,
        started_at=datetime.now(UTC),
        active_objects=step.objects_involved,
    )
    detection_context = build_detection_context(session_context, recipe)
    print(f"step={step.id}; prompts={len(detection_context.prompts)}")
    print(", ".join(detection_context.prompts))
    print("green=primary, orange=confuser, blue=hand anchor")

    detector = ObjectDetector(device=args.device, conf=args.raw_conf)
    ContextualVocabularyController(detector).sync(detection_context)
    capture = _open_capture(_source(args.source))
    started = time.monotonic()
    frame_number = 0
    latest: list[ContextDetection] = []
    previous_signature: tuple[tuple[str, float], ...] = ()
    color_text: str | None = None
    previous_color_state: str | None = None

    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                raise RuntimeError("camera/video frame read failed")
            frame_number += 1
            if frame_number % args.detect_every == 1:
                raw = detector.detect(frame)
                latest = canonicalize_detections(raw, detection_context)
                signature = tuple(
                    (item.canonical_label, round(item.conf, 2)) for item in latest
                )
                if signature != previous_signature:
                    print(
                        "detections:",
                        signature or "none",
                        f"({detector.last_latency_ms:.0f} ms)",
                    )
                    previous_signature = signature

                if args.color_signals:
                    wok = next(
                        (
                            item
                            for item in latest
                            if item.canonical_label == "wok" and item.role == "primary"
                        ),
                        None,
                    )
                    if wok is None:
                        color_text = "color ROI: waiting for wok"
                    else:
                        signals = extract_tomato_egg_color_signals(frame, wok.box)
                        color_text = (
                            f"color={signals.state} red={signals.red_ratio:.2f} "
                            f"yellow={signals.yellow_ratio:.2f}"
                        )
                        if signals.state != previous_color_state:
                            print(color_text)
                            previous_color_state = signals.state

            if not args.no_display:
                _draw(frame, latest, detector.last_latency_ms, color_text)
                cv2.imshow("NomaChef context detection", frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
            if args.duration > 0 and time.monotonic() - started >= args.duration:
                break
    finally:
        capture.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
