"""Periodic keyframe snapshots and timeline diffs (the 3-5s state ledger)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class StateSnapshot:
    pts_ms: float
    frame_idx: int
    step_id: str
    context_version: int
    score: float
    pending_question: str | None
    detections: tuple[tuple[str, float], ...]
    color_state: str | None


class KeyframeSampler:
    """Fire on the first frame, then once per interval of video time."""

    def __init__(self, interval_ms: float) -> None:
        if interval_ms <= 0:
            raise ValueError("interval_ms must be positive")
        self.interval_ms = interval_ms
        self._next_at = 0.0

    def due(self, pts_ms: float) -> bool:
        if pts_ms < self._next_at:
            return False
        self._next_at = pts_ms + self.interval_ms
        return True


def diff_snapshots(prev: StateSnapshot | None, cur: StateSnapshot) -> dict:
    if prev is None:
        return {"baseline": True}
    prev_objs = {label for label, _conf in prev.detections}
    cur_objs = {label for label, _conf in cur.detections}
    return {
        "step_changed": cur.step_id != prev.step_id,
        "score_delta": round(cur.score - prev.score, 4),
        "objects_appeared": sorted(cur_objs - prev_objs),
        "objects_gone": sorted(prev_objs - cur_objs),
        "color_changed": cur.color_state != prev.color_state,
    }


def keyframe_row(cur: StateSnapshot, diff: dict, jpg_name: str) -> dict:
    row = asdict(cur)
    row["detections"] = [list(item) for item in cur.detections]
    row["jpg"] = jpg_name
    row["diff"] = diff
    return row


def append_jsonl(path: Path, row: dict) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
