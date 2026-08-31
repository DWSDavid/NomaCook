"""Minimal annotation loader for frame-level expected-step lookup.

Consumes a YAML with timestamped segments. Produces per-frame
expected_step_id lookups. No annotations flow into StateEngine.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import yaml


@dataclass(frozen=True)
class AnnotationSegment:
    start_s: float
    end_s: float
    expected_step_id: str
    label_zh: str = ""


@dataclass(frozen=True)
class FrameAnnotation:
    expected_step_id: str
    label_zh: str = ""


class AnnotationTimeline:
    """Ordered, non-overlapping segments for per-frame expected-step lookup."""

    def __init__(self, segments: Sequence[AnnotationSegment]) -> None:
        self.segments = tuple(segments)

    def lookup(self, pts_ms: float) -> FrameAnnotation | None:
        t_s = pts_ms / 1000.0
        for seg in self.segments:
            if seg.start_s <= t_s < seg.end_s:
                return FrameAnnotation(
                    expected_step_id=seg.expected_step_id,
                    label_zh=seg.label_zh,
                )
        return None


def load_annotations(path: str | Path, valid_step_ids: set[str] | None = None) -> AnnotationTimeline:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    segments: list[AnnotationSegment] = []
    for i, seg in enumerate(raw["segments"]):
        start = float(seg["start_s"])
        end = float(seg["end_s"])
        if start < 0:
            raise ValueError(f"segment {i}: start_s must be >= 0, got {start}")
        if end <= start:
            raise ValueError(f"segment {i}: end_s ({end}) must be > start_s ({start})")
        step_id = seg["expected_step_id"]
        if valid_step_ids is not None and step_id not in valid_step_ids:
            raise ValueError(
                f"segment {i}: unknown step_id {step_id!r}; known={sorted(valid_step_ids)}"
            )
        segments.append(AnnotationSegment(
            start_s=start, end_s=end,
            expected_step_id=step_id,
            label_zh=seg.get("label_zh", ""),
        ))

    # check non-overlapping
    segments.sort(key=lambda s: s.start_s)
    for i in range(len(segments) - 1):
        if segments[i].end_s > segments[i + 1].start_s:
            raise ValueError(
                f"overlapping segments: {i} ends at {segments[i].end_s}s "
                f"but {i + 1} starts at {segments[i + 1].start_s}s"
            )

    return AnnotationTimeline(segments)
