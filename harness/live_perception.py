"""Live Layer-1 perception loop: webcam (or video) -> YOLO-World + MediaPipe
-> interaction events -> JSONL session log + on-screen overlay.

Usage:
    python harness/live_perception.py                       # webcam 0
    python harness/live_perception.py --source path.mp4     # replay footage
    python harness/live_perception.py --no-display          # headless
    python harness/live_perception.py --vocab "wok,bowl,bottle,egg"

Keys: q quit, s force a snapshot line.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from perception.detector import DEFAULT_VOCAB, ObjectDetector
from perception.fusion import InteractionTracker
from perception.hands import HandTracker
from perception.session_logger import SessionLogger

DETECT_EVERY = 3  # run YOLO every Nth frame; hands run every frame
SNAPSHOT_EVERY_SEC = 5.0

HAND_EDGES = [  # skeleton for drawing
    (0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 9), (9, 10), (10, 11), (11, 12), (9, 13), (13, 14), (14, 15), (15, 16),
    (13, 17), (17, 18), (18, 19), (19, 20), (0, 17),
]


def draw_overlay(frame, detections, hands, active_pairs, fps):
    for d in detections:
        x1, y1, x2, y2 = d.box
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 200, 0), 2)
        cv2.putText(frame, f"{d.label} {d.conf:.2f}", (x1, max(y1 - 6, 12)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 0), 1)
    for hs in hands:
        pts = hs.landmarks_px.astype(int)
        for a, b in HAND_EDGES:
            cv2.line(frame, tuple(pts[a]), tuple(pts[b]), (255, 160, 0), 1)
        for p in pts:
            cv2.circle(frame, tuple(p), 2, (255, 160, 0), -1)
        x1, y1, _, _ = hs.box
        state = "GRIP" if hs.is_gripping else "open"
        cv2.putText(frame, f"{hs.handedness} {state} {hs.grip_closure:.2f}",
                    (x1, max(y1 - 6, 12)), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (255, 160, 0), 1)
    y = 22
    cv2.putText(frame, f"{fps:.1f} FPS", (8, y), cv2.FONT_HERSHEY_SIMPLEX,
                0.6, (255, 255, 255), 2)
    for (hand, obj), kind in active_pairs.items():
        y += 22
        color = (0, 80, 255) if kind == "holding" else (0, 200, 255)
        cv2.putText(frame, f"{hand} hand {kind} {obj}", (8, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
    return frame


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="0", help="camera index or video path")
    ap.add_argument("--vocab", default=None, help="comma-separated object list")
    ap.add_argument("--no-display", action="store_true")
    ap.add_argument("--max-frames", type=int, default=0,
                    help="stop after N frames (0 = run until q / EOF)")
    args = ap.parse_args()

    vocab = ([v.strip() for v in args.vocab.split(",")] if args.vocab
             else DEFAULT_VOCAB)
    source = int(args.source) if args.source.isdigit() else args.source

    detector = ObjectDetector(vocab=vocab)
    hand_tracker = HandTracker()
    fusion = InteractionTracker(k_frames=3)

    # On macOS, CAP_ANY may prefer the FFMPEG backend.  Its camera-index
    # ordering can differ from AVFoundation when Continuity Camera is present,
    # so an index of 0 may open the iPhone instead of the built-in camera.
    backend = (cv2.CAP_AVFOUNDATION
               if sys.platform == "darwin" and isinstance(source, int)
               else cv2.CAP_ANY)
    cap = cv2.VideoCapture(source, backend)
    if not cap.isOpened():
        sys.exit(f"cannot open source: {args.source}")
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps_src = cap.get(cv2.CAP_PROP_FPS)
    frame_ms = 1000.0 / fps_src if fps_src and fps_src > 0 else None

    logger = SessionLogger(
        meta={"source": str(args.source), "vocab": vocab, "resolution": [w, h]}
    )
    print(f"session log -> {logger.path}")

    detections = []
    active_pairs: dict[tuple[str, str], str] = {}
    frame_idx = 0
    last_snapshot = 0.0
    fps, t_prev = 0.0, time.perf_counter()

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            now = time.time()

            if frame_idx % DETECT_EVERY == 0:
                detections = detector.detect(frame)
            pts_ms = frame_idx * frame_ms if frame_ms else None
            hands = hand_tracker.detect(frame, timestamp_ms=pts_ms)

            events = fusion.update(
                t=(pts_ms / 1000.0) if pts_ms is not None else now,
                frame=frame_idx,
                hands=[(hs.handedness, hs.palm_center, hs.box, hs.is_gripping)
                       for hs in hands],
                detections=[(d.label, d.conf, d.box) for d in detections],
            )
            for ev in events:
                logger.log_event(ev)
                print(f"[{ev.frame:>6}] {ev.event}: {ev.hand} / {ev.object}")
                key = (ev.hand, ev.object)
                if ev.event.endswith("_end"):
                    active_pairs.pop(key, None)
                else:
                    active_pairs[key] = ev.event.split("_")[1]  # near/holding

            if now - last_snapshot >= SNAPSHOT_EVERY_SEC:
                last_snapshot = now
                logger.log_snapshot(
                    now, frame_idx,
                    detections=[{"label": d.label, "conf": round(d.conf, 3),
                                 "box": d.box} for d in detections],
                    hands=[{"handedness": hs.handedness, "box": hs.box,
                            "grip_closure": round(hs.grip_closure, 3)}
                           for hs in hands],
                )

            t_now = time.perf_counter()
            fps = 0.9 * fps + 0.1 * (1.0 / max(t_now - t_prev, 1e-6))
            t_prev = t_now

            if not args.no_display:
                draw_overlay(frame, detections, hands, active_pairs, fps)
                cv2.imshow("SousAI Layer-1", frame)
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    break
                if key == ord("s"):
                    last_snapshot = 0.0  # force snapshot next loop
            frame_idx += 1
            if args.max_frames and frame_idx >= args.max_frames:
                break
    finally:
        cap.release()
        hand_tracker.close()
        logger.close()
        if not args.no_display:
            cv2.destroyAllWindows()
        print(f"session log saved: {logger.path}")


if __name__ == "__main__":
    main()
