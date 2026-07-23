"""YOLO-World open-vocabulary object detection wrapper.

Perception modules eat ndarrays and return dataclasses. No cameras, no GUI —
that all lives in harness/. Swap the frame source (webcam, video file, Pi
MJPEG stream) without touching this file.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ultralytics import YOLOWorld

from perception.kitchen_vocab import vocab_for_step

# Default kitchen vocabulary for a fried-rice-class dish. In the real flow the
# vocab comes from the current SOP step's `objects_involved` (English only —
# YOLO-World's text encoder is English CLIP), expanded via
# kitchen_vocab.vocab_for_step().
DEFAULT_VOCAB = vocab_for_step(
    ["spatula", "cutting board", "knife", "bowl", "plate", "bottle", "egg", "scallion", "garlic"]
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WEIGHTS = _REPO_ROOT / "yolov8s-worldv2.pt"


@dataclass
class Detection:
    label: str
    conf: float
    box: tuple[int, int, int, int]  # xyxy, pixels

    @property
    def center(self) -> tuple[float, float]:
        x1, y1, x2, y2 = self.box
        return ((x1 + x2) / 2, (y1 + y2) / 2)


class ObjectDetector:
    """YOLO-World with a per-SOP-step vocabulary.

    Call set_vocab() whenever the active step changes; detect() on frames.
    """

    def __init__(
        self,
        weights: str | Path = DEFAULT_WEIGHTS,
        vocab: list[str] | None = None,
        device: str = "mps",
        conf: float = 0.15,
    ) -> None:
        self.model = YOLOWorld(str(weights))
        self.device = device
        self.conf = conf
        self.vocab: list[str] = []
        self.set_vocab(vocab or DEFAULT_VOCAB)
        self.last_latency_ms: float = 0.0

    def set_vocab(self, vocab: list[str]) -> None:
        if vocab != self.vocab:
            self.vocab = list(vocab)
            self.model.set_classes(self.vocab)

    def detect(self, frame_bgr: np.ndarray) -> list[Detection]:
        t0 = time.perf_counter()
        results = self.model.predict(
            frame_bgr, conf=self.conf, device=self.device, verbose=False
        )
        self.last_latency_ms = (time.perf_counter() - t0) * 1000

        detections: list[Detection] = []
        r = results[0]
        for box in r.boxes:
            x1, y1, x2, y2 = (int(v) for v in box.xyxy[0].tolist())
            detections.append(
                Detection(
                    label=r.names[int(box.cls)],
                    conf=float(box.conf),
                    box=(x1, y1, x2, y2),
                )
            )
        return detections
