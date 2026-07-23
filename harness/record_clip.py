"""Record a short webcam clip into data/test_videos/ for pipeline testing.

Usage:
    .venv/bin/python harness/record_clip.py                    # 90s max, q to stop
    .venv/bin/python harness/record_clip.py --duration 60 --name kitchen_live_test
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "data" / "test_videos"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default="0", help="camera index")
    ap.add_argument("--duration", type=float, default=90.0, help="max seconds")
    ap.add_argument("--name", default=None, help="output stem (default: timestamp)")
    ap.add_argument("--fps", type=float, default=15.0, help="recording fps")
    args = ap.parse_args()

    source = int(args.source) if args.source.isdigit() else args.source
    backend = (cv2.CAP_AVFOUNDATION
               if sys.platform == "darwin" and isinstance(source, int)
               else cv2.CAP_ANY)
    cap = cv2.VideoCapture(source, backend)
    if not cap.isOpened():
        sys.exit(f"cannot open camera {args.source}")
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = args.name or time.strftime("clip_%Y%m%dT%H%M%S")
    out_path = OUT_DIR / f"{stem}.mp4"
    writer = cv2.VideoWriter(str(out_path), cv2.VideoWriter_fourcc(*"mp4v"),
                             args.fps, (width, height))
    if not writer.isOpened():
        cap.release()
        sys.exit(f"cannot open writer for {out_path}")

    print(f"recording -> {out_path}  ({width}x{height} @ {args.fps:.0f}fps, "
          f"max {args.duration:.0f}s, press q to stop)")
    frame_interval = 1.0 / args.fps
    started = time.monotonic()
    next_grab = started
    frames = 0
    try:
        while time.monotonic() - started < args.duration:
            ok, frame = cap.read()
            if not ok:
                break
            now = time.monotonic()
            if now >= next_grab:  # decimate camera fps down to recording fps
                writer.write(frame)
                frames += 1
                next_grab = now + frame_interval
            elapsed = now - started
            cv2.putText(frame, f"REC {elapsed:5.1f}s  q=stop", (16, 36),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 255), 2)
            cv2.imshow("NomaChef recorder", frame)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
    finally:
        cap.release()
        writer.release()
        cv2.destroyAllWindows()
    print(f"saved {frames} frames ({frames / args.fps:.1f}s video) -> {out_path}")


if __name__ == "__main__":
    main()
