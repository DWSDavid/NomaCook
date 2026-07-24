"""Overlay pipeline state onto frames and write the annotated MP4."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import cv2
import numpy as np

ROLE_COLORS = {"primary": (0, 220, 0), "anchor": (255, 160, 0),
               "confuser": (0, 210, 255)}
_FONT = cv2.FONT_HERSHEY_SIMPLEX
_CJK_FONT_CANDIDATES = (
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
)

HAND_EDGES = [
    (0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 9), (9, 10), (10, 11), (11, 12), (9, 13), (13, 14), (14, 15), (15, 16),
    (13, 17), (17, 18), (18, 19), (19, 20), (0, 17),
]
HAND_COLOR = (255, 160, 0)

try:  # Pillow ships with ultralytics; optional CJK banner support.
    from PIL import Image, ImageDraw, ImageFont
    _CJK_FONT_PATH = next(
        (path for path in _CJK_FONT_CANDIDATES if Path(path).exists()), None
    )
    _CJK_FONT = (
        ImageFont.truetype(_CJK_FONT_PATH, 18) if _CJK_FONT_PATH else None
    )
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
    hands: Sequence = (),
    step_sequence: int | None = None,
    total_steps: int | None = None,
    step_title: str = "",
) -> None:
    height, width = frame_bgr.shape[:2]
    cv2.rectangle(frame_bgr, (0, 0), (width, 58), (32, 32, 32), -1)

    for det in detections:
        x1, y1, x2, y2 = det.box
        color = ROLE_COLORS.get(det.role, (200, 200, 200))
        cv2.rectangle(frame_bgr, (x1, y1), (x2, y2), color, 2)
        cv2.putText(frame_bgr, f"{det.canonical_label} {det.conf:.2f}",
                    (x1, max(70, y1 - 6)), _FONT, 0.5, color, 1)

    for hand in hands:
        pts = hand.landmarks_px.astype(int)
        for a, b in HAND_EDGES:
            cv2.line(frame_bgr, tuple(pts[a]), tuple(pts[b]), HAND_COLOR, 1)
        for p in pts:
            cv2.circle(frame_bgr, tuple(p), 2, HAND_COLOR, -1)
        x1, y1 = pts.min(axis=0)
        state = "GRIP" if hand.is_gripping else "open"
        cv2.putText(frame_bgr,
                    f"{hand.handedness} {state} {hand.grip_closure:.2f}",
                    (int(x1), max(70, int(y1) - 6)), _FONT, 0.5, HAND_COLOR, 1)

    bar_w = int((width - 16) * min(score / max(threshold, 1e-6), 1.0))
    cv2.rectangle(frame_bgr, (8, 48), (8 + bar_w, 54), (0, 220, 0), -1)
    cv2.rectangle(frame_bgr, (8, 48), (width - 8, 54), (90, 90, 90), 1)

    if step_sequence is not None and total_steps is not None:
        heading = f"第 {step_sequence}/{total_steps} 步"
        if step_title:
            heading += f" · {step_title}"
        heading += f"  score {score:.2f}/{threshold:.2f}"
    else:
        heading = f"{step_id}  score {score:.2f}/{threshold:.2f}"
    lines = [heading, instruction]
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
