"""Overlay pipeline state onto frames and write the annotated MP4."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import cv2
import numpy as np

ROLE_COLORS = {"primary": (0, 220, 0), "anchor": (255, 160, 0),
               "confuser": (0, 210, 255)}
_FONT = cv2.FONT_HERSHEY_SIMPLEX
_PINGFANG = "/System/Library/Fonts/PingFang.ttc"

try:  # Pillow ships with ultralytics; optional CJK banner support.
    from PIL import Image, ImageDraw, ImageFont
    _CJK_FONT = ImageFont.truetype(_PINGFANG, 18) if Path(_PINGFANG).exists() else None
except Exception:  # pragma: no cover - environment without Pillow
    _CJK_FONT = None


def _banner_text(frame: np.ndarray, lines: list[str]) -> None:
    if _CJK_FONT is not None:
        image = Image.fromarray(frame[..., ::-1])
        draw = ImageDraw.Draw(image)
        for i, line in enumerate(lines):
            draw.text((8, 6 + 24 * i), line, font=_CJK_FONT, fill=(255, 255, 255))
        frame[:] = np.asarray(image)[..., ::-1]
    else:
        for i, line in enumerate(lines):
            ascii_line = line.encode("ascii", "replace").decode()
            cv2.putText(frame, ascii_line, (8, 24 + 24 * i), _FONT, 0.6,
                        (255, 255, 255), 2)


def draw_overlay(
    frame_bgr: np.ndarray,
    *,
    detections: Sequence,
    step_id: str,
    instruction: str,
    score: float,
    threshold: float,
    pending_question: str | None,
    recent_events: Sequence[str],
    color_text: str | None,
) -> None:
    height, width = frame_bgr.shape[:2]
    cv2.rectangle(frame_bgr, (0, 0), (width, 58), (32, 32, 32), -1)

    for det in detections:
        x1, y1, x2, y2 = det.box
        color = ROLE_COLORS.get(det.role, (200, 200, 200))
        cv2.rectangle(frame_bgr, (x1, y1), (x2, y2), color, 2)
        cv2.putText(frame_bgr, f"{det.canonical_label} {det.conf:.2f}",
                    (x1, max(70, y1 - 6)), _FONT, 0.5, color, 1)

    bar_w = int((width - 16) * min(score / max(threshold, 1e-6), 1.0))
    cv2.rectangle(frame_bgr, (8, 48), (8 + bar_w, 54), (0, 220, 0), -1)
    cv2.rectangle(frame_bgr, (8, 48), (width - 8, 54), (90, 90, 90), 1)

    lines = [f"{step_id}  score {score:.2f}/{threshold:.2f}", instruction]
    if pending_question:
        lines[1] = f"? {pending_question}"
    _banner_text(frame_bgr, lines)

    y = height - 10
    for text in list(recent_events)[-3:]:
        cv2.putText(frame_bgr, text, (8, y), _FONT, 0.45, (0, 200, 255), 1)
        y -= 18
    if color_text:
        cv2.putText(frame_bgr, color_text, (8, 74), _FONT, 0.5, (0, 255, 255), 1)


class AnnotatedVideoWriter:
    def __init__(self, path: Path, fps: float, frame_size: tuple[int, int]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._writer = cv2.VideoWriter(
            str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, frame_size)
        if not self._writer.isOpened():
            raise RuntimeError(f"cannot open VideoWriter for {path}")
        self._size = frame_size
        self.frames_written = 0

    def write(self, frame_bgr: np.ndarray) -> None:
        height, width = frame_bgr.shape[:2]
        if (width, height) != self._size:
            frame_bgr = cv2.resize(frame_bgr, self._size)
        self._writer.write(frame_bgr)
        self.frames_written += 1

    def close(self) -> None:
        self._writer.release()
