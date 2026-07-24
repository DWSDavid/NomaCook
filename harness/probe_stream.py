"""Probe an ESP32 (or any) MJPEG stream URL: can we read frames, at what size
and rate. Run this BEFORE the full pipeline to isolate "is the camera
reachable" from "does the perception work".

Usage:
    .venv/bin/python harness/probe_stream.py --url http://192.168.x.x:81/stream

Fails fast on a bad URL (no reconnect loop) so a typo doesn't hang.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", required=True, help="MJPEG stream URL")
    ap.add_argument("--frames", type=int, default=30, help="frames to sample")
    ap.add_argument("--save", default=None, help="write first frame to this path")
    args = ap.parse_args()

    print(f"opening {args.url} ...")
    cap = cv2.VideoCapture(args.url)
    if not cap.isOpened():
        sys.exit(
            "FAILED to open the stream. Check:\n"
            "  - laptop and ESP32 on the SAME Wi-Fi (a phone hotspot is safest)\n"
            "  - the URL/port is right (browser: does http://<ip>/ show video?)\n"
            "  - the stream path is usually :81/stream")

    read = 0
    t0 = time.time()
    first = None
    for _ in range(args.frames):
        ok, frame = cap.read()
        if not ok:
            break
        if first is None:
            first = frame
        read += 1
    dt = time.time() - t0
    cap.release()

    if read == 0:
        sys.exit("opened the URL but read 0 frames — wrong path or the board "
                 "isn't actually streaming. Try http://<ip>:81/stream")

    h, w = first.shape[:2]
    print(f"OK: read {read} frames, {w}x{h}, ~{read / max(dt, 1e-6):.1f} FPS")
    if args.save:
        cv2.imwrite(args.save, first)
        print(f"first frame saved -> {args.save}")
    print("\nstream works. now run the live demo with:")
    print(f"  .venv/bin/python harness/live_demo.py --source {args.url}")


if __name__ == "__main__":
    main()
