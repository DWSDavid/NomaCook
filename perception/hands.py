"""MediaPipe HandLandmarker wrapper: 21 landmarks -> HandState dataclasses.

mediapipe >= 0.10.30 removed the legacy mp.solutions API, so this uses the
Tasks API (VIDEO mode). Model file: weights/hand_landmarker.task, from
https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/latest/hand_landmarker.task

Same contract as detector.py: ndarray in, dataclasses out, no GUI.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import mediapipe as mp
import numpy as np
from mediapipe.tasks.python.core.base_options import BaseOptions
from mediapipe.tasks.python import vision

# Landmark indices (MediaPipe hand model)
WRIST = 0
THUMB_TIP = 4
INDEX_MCP = 5
MIDDLE_MCP = 9
FINGERTIPS = (4, 8, 12, 16, 20)

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = _REPO_ROOT / "weights" / "hand_landmarker.task"


@dataclass
class HandState:
    handedness: str  # "Left" / "Right" (as seen by the camera)
    landmarks_px: np.ndarray  # (21, 2) pixel coords
    box: tuple[int, int, int, int]  # xyxy bounding box of landmarks
    grip_closure: float  # 0 = wide open, ~1 = fully closed fist/grip

    @property
    def is_gripping(self) -> bool:
        # Threshold picked by waving objects at a webcam; revisit with real
        # first-person footage. Downstream treats this as weak evidence anyway.
        return self.grip_closure > 0.55

    @property
    def palm_center(self) -> tuple[float, float]:
        pts = self.landmarks_px[[WRIST, INDEX_MCP, MIDDLE_MCP]]
        return (float(pts[:, 0].mean()), float(pts[:, 1].mean()))


def grip_closure(landmarks_px: np.ndarray) -> float:
    """How closed the hand is, scale-invariant.

    Mean fingertip distance to palm center, normalized by hand size
    (wrist -> middle MCP). Open hand ~1.3-1.8, closed grip ~0.5-0.9.
    Mapped so that the returned value grows with closure and lands in [0, 1].
    """
    palm = landmarks_px[[WRIST, INDEX_MCP, MIDDLE_MCP]].mean(axis=0)
    hand_size = float(np.linalg.norm(landmarks_px[MIDDLE_MCP] - landmarks_px[WRIST]))
    if hand_size < 1e-6:
        return 0.0
    tip_dist = float(
        np.mean([np.linalg.norm(landmarks_px[i] - palm) for i in FINGERTIPS])
    )
    ratio = tip_dist / hand_size  # open ~1.5, closed ~0.7
    return float(np.clip((1.6 - ratio) / 0.9, 0.0, 1.0))


class HandTracker:
    """VIDEO-mode HandLandmarker. Feed frames in order; timestamps handled
    internally (Tasks API requires monotonically increasing ms timestamps)."""

    def __init__(
        self,
        model_path: str | Path = DEFAULT_MODEL,
        max_hands: int = 2,
        min_det_conf: float = 0.5,
    ) -> None:
        options = vision.HandLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(model_path)),
            running_mode=vision.RunningMode.VIDEO,
            num_hands=max_hands,
            min_hand_detection_confidence=min_det_conf,
            min_tracking_confidence=0.5,
        )
        self._landmarker = vision.HandLandmarker.create_from_options(options)
        self._ts_ms = 0

    def detect(
        self, frame_bgr: np.ndarray, timestamp_ms: float | None = None
    ) -> list[HandState]:
        h, w = frame_bgr.shape[:2]
        rgb = np.ascontiguousarray(frame_bgr[:, :, ::-1])
        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        if timestamp_ms is None:
            self._ts_ms += 33  # legacy webcam path: nominal 30 fps clock
        else:
            # Real frame time from the caller; Tasks API only needs strict
            # monotonic int milliseconds, so ceil and bump on collisions.
            candidate = int(math.ceil(timestamp_ms))
            self._ts_ms = max(candidate, self._ts_ms + 1)
        result = self._landmarker.detect_for_video(image, self._ts_ms)

        states: list[HandState] = []
        for lm_list, handed in zip(result.hand_landmarks, result.handedness):
            pts = np.array([(lm.x * w, lm.y * h) for lm in lm_list])
            x1, y1 = pts.min(axis=0)
            x2, y2 = pts.max(axis=0)
            states.append(
                HandState(
                    handedness=handed[0].category_name,
                    landmarks_px=pts,
                    box=(int(x1), int(y1), int(x2), int(y2)),
                    grip_closure=grip_closure(pts),
                )
            )
        return states

    @property
    def last_timestamp_ms(self) -> int:
        return self._ts_ms

    def close(self) -> None:
        self._landmarker.close()
