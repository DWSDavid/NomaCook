"""Cheap HSV evidence for the visually distinctive tomato-and-egg demo."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import cv2
import numpy as np

from server.events import EventEnvelope, create_event


ColorState = Literal[
    "red_dominant", "yellow_dominant", "red_yellow_mixed", "uncertain"
]


@dataclass(frozen=True)
class TomatoEggColorSignals:
    red_ratio: float
    yellow_ratio: float
    colorful_ratio: float
    state: ColorState
    confidence: float

    def payload(self, step_id: str) -> dict[str, float | str]:
        return {
            "step_id": step_id,
            "state": self.state,
            "red_ratio": round(self.red_ratio, 4),
            "yellow_ratio": round(self.yellow_ratio, 4),
            "colorful_ratio": round(self.colorful_ratio, 4),
        }


def _bounded_roi(
    frame: np.ndarray, roi: tuple[int, int, int, int] | None
) -> np.ndarray:
    if frame.ndim != 3 or frame.shape[2] != 3:
        raise ValueError("frame must be a BGR image with shape HxWx3")
    if roi is None:
        return frame
    height, width = frame.shape[:2]
    x1, y1, x2, y2 = roi
    x1, x2 = max(0, min(x1, width)), max(0, min(x2, width))
    y1, y2 = max(0, min(y1, height)), max(0, min(y2, height))
    if x2 <= x1 or y2 <= y1:
        raise ValueError("ROI does not overlap the frame")
    return frame[y1:y2, x1:x2]


def extract_tomato_egg_color_signals(
    frame_bgr: np.ndarray,
    roi: tuple[int, int, int, int] | None = None,
) -> TomatoEggColorSignals:
    """Measure color evidence; this intentionally does not decide doneness."""

    crop = _bounded_roi(frame_bgr, roi)
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    red_low = cv2.inRange(hsv, (0, 80, 50), (12, 255, 255))
    red_high = cv2.inRange(hsv, (168, 80, 50), (179, 255, 255))
    red_mask = cv2.bitwise_or(red_low, red_high)
    yellow_mask = cv2.inRange(hsv, (15, 60, 70), (42, 255, 255))
    colorful_mask = cv2.inRange(hsv, (0, 55, 45), (179, 255, 255))

    pixels = float(crop.shape[0] * crop.shape[1])
    red_ratio = cv2.countNonZero(red_mask) / pixels
    yellow_ratio = cv2.countNonZero(yellow_mask) / pixels
    colorful_ratio = cv2.countNonZero(colorful_mask) / pixels

    if red_ratio >= 0.06 and yellow_ratio >= 0.06:
        state: ColorState = "red_yellow_mixed"
        confidence = min(1.0, (red_ratio + yellow_ratio) / 0.35)
    elif yellow_ratio >= 0.12 and red_ratio < 0.05:
        state = "yellow_dominant"
        confidence = min(1.0, yellow_ratio / 0.30)
    elif red_ratio >= 0.12 and yellow_ratio < 0.05:
        state = "red_dominant"
        confidence = min(1.0, red_ratio / 0.30)
    else:
        state = "uncertain"
        confidence = min(0.59, (red_ratio + yellow_ratio) / 0.20)

    return TomatoEggColorSignals(
        red_ratio=red_ratio,
        yellow_ratio=yellow_ratio,
        colorful_ratio=colorful_ratio,
        state=state,
        confidence=confidence,
    )


def create_color_evidence_event(
    signals: TomatoEggColorSignals,
    *,
    session_id: str,
    seq: int,
    step_id: str,
    frame_id: str,
    t_device_ms: float,
    t_server_est: datetime,
    received_at: datetime,
) -> EventEnvelope:
    return create_event(
        session_id=session_id,
        seq=seq,
        event_type="perception.roi_color",
        t_device_ms=t_device_ms,
        t_server_est=t_server_est,
        received_at=received_at,
        frame_id=frame_id,
        source="opencv_hsv_tomato_egg_v1",
        confidence=signals.confidence,
        payload=signals.payload(step_id),
    )
