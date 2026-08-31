"""Frame sources: the one seam that separates "where frames come from" from
"what we do with them".

The offline pipeline reads a video file; the live service reads the ESP32
camera's MJPEG stream. Both yield the same thing: (pts_ms, bgr_frame). The
whole perception + scoring brain downstream never knows the difference, so it
stays identical between the offline test harness and the real-time service.

pts_ms convention:
- VideoFileSource: deterministic, frame_index / fps * 1000. Replays are
  bit-for-bit reproducible (the property the offline base relies on).
- CameraStreamSource: wall-clock milliseconds since the first frame. Real
  time is what matters live; there is no "frame index" to trust.

Reference: docs/NOMACHEF-TECHNICAL-SPEC.md §4.2 (ESP32 streams MJPEG/WS JPEG,
640x480, 5-10 FPS; reconnect + resource release must be verified for long runs).
"""

from __future__ import annotations

import time
from typing import Iterator, Protocol

import cv2
import numpy as np


class FrameSource(Protocol):
    def frames(self) -> Iterator[tuple[float, np.ndarray]]:
        """Yield (pts_ms, bgr_frame) until the source ends or is closed."""
        ...

    def close(self) -> None:
        ...


class VideoFileSource:
    """Offline: a video file, deterministic timestamps. Mirrors what
    harness/run_pipeline.py does today so the offline base is unchanged."""

    def __init__(self, path: str) -> None:
        self._cap = cv2.VideoCapture(path)
        if not self._cap.isOpened():
            raise RuntimeError(f"cannot open video file: {path}")
        self.fps = self._cap.get(cv2.CAP_PROP_FPS) or 30.0
        self.width = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    def frames(self) -> Iterator[tuple[float, np.ndarray]]:
        frame_ms = 1000.0 / self.fps
        idx = 0
        while True:
            ok, frame = self._cap.read()
            if not ok:
                return
            yield idx * frame_ms, frame
            idx += 1

    def close(self) -> None:
        self._cap.release()


class CameraStreamSource:
    """Live: an MJPEG-over-HTTP (or any OpenCV-openable) stream, e.g. the
    DFRobot ESP32-S3 CameraWebServer URL. Wall-clock timestamps, with a
    reconnect loop so a dropped Wi-Fi frame never ends the session.

    Deliberately thin: the ESP32 does JPEG; OpenCV decodes. Backpressure and
    "run inference off the socket thread" belong to the gateway, not here."""

    def __init__(
        self,
        url: str,
        *,
        reconnect_backoff_s: float = 1.0,
        max_backoff_s: float = 8.0,
        read_timeout_s: float = 5.0,
        stall_giveup_s: float | None = None,
    ) -> None:
        self.url = url
        self._reconnect_backoff_s = reconnect_backoff_s
        self._max_backoff_s = max_backoff_s
        self._read_timeout_s = read_timeout_s
        self._stall_giveup_s = stall_giveup_s
        self._cap: cv2.VideoCapture | None = None
        self._closed = False
        self._t0: float | None = None

    def _open(self) -> None:
        if self._cap is not None:
            self._cap.release()
        cap = cv2.VideoCapture(self.url)
        try:
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, int(self._read_timeout_s * 1000))
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, int(self._read_timeout_s * 1000))
        except AttributeError:
            pass  # older OpenCV: no timeout props, rely on the reconnect loop
        self._cap = cap

    def frames(self) -> Iterator[tuple[float, np.ndarray]]:
        backoff = self._reconnect_backoff_s
        last_good = time.monotonic()
        self._open()
        while not self._closed:
            cap = self._cap
            ok, frame = (cap.read() if cap is not None and cap.isOpened()
                         else (False, None))
            now = time.monotonic()
            if ok and frame is not None:
                if self._t0 is None:
                    self._t0 = now
                last_good = now
                backoff = self._reconnect_backoff_s
                yield (now - self._t0) * 1000.0, frame
                continue

            if (self._stall_giveup_s is not None
                    and now - last_good > self._stall_giveup_s):
                raise RuntimeError(
                    f"camera stream stalled > {self._stall_giveup_s}s: {self.url}")
            print(f"[camera] read failed, reconnecting in {backoff:.1f}s: {self.url}")
            time.sleep(backoff)
            backoff = min(backoff * 2, self._max_backoff_s)
            self._open()

    def close(self) -> None:
        self._closed = True
        if self._cap is not None:
            self._cap.release()
            self._cap = None


class WebcamSource:
    """Live: a local USB/built-in webcam by index (0, 1, ...). Wall-clock
    timestamps like the network stream, but no reconnect loop: a failed read
    on a local device means it was unplugged, so we stop cleanly."""

    def __init__(self, index: int) -> None:
        self.index = index
        self._cap = cv2.VideoCapture(index)
        if not self._cap.isOpened():
            raise RuntimeError(
                f"cannot open webcam index {index}; try a different --source "
                f"(0/1/...) or grant camera permission")
        self.width = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    def frames(self) -> Iterator[tuple[float, np.ndarray]]:
        t0: float | None = None
        while True:
            ok, frame = self._cap.read()
            if not ok:
                return
            now = time.monotonic()
            if t0 is None:
                t0 = now
            yield (now - t0) * 1000.0, frame

    def close(self) -> None:
        self._cap.release()


def open_source(spec: str, **kwargs) -> FrameSource:
    """Pick a source from a string:
    - all digits ("0", "1")     -> local webcam by index
    - http/https/rtsp URL       -> network camera stream (e.g. ESP32 MJPEG)
    - anything else             -> local video file
    One call site works offline (file) or live (webcam / ESP32)."""
    if spec.isdigit():
        return WebcamSource(int(spec))
    if spec.startswith(("http://", "https://", "rtsp://")):
        return CameraStreamSource(spec, **kwargs)
    return VideoFileSource(spec)
