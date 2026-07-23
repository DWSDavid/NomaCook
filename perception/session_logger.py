"""JSONL session logger — the embryo of the Supabase `step_events` table.

DEPRECATED for pipeline use: harness/run_pipeline.py writes validated
EventEnvelope JSONL via server.events; this legacy flat format remains only
for harness/live_perception.py webcam smoke runs.

One file per live session under data/sessions/. Every line is one JSON event:
session_start / session_end, interaction events, and periodic snapshots of all
detections (for offline threshold tuning and replay).
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from perception.fusion import InteractionEvent

_REPO_ROOT = Path(__file__).resolve().parent.parent
SESSIONS_DIR = _REPO_ROOT / "data" / "sessions"


class SessionLogger:
    def __init__(self, meta: dict[str, Any] | None = None) -> None:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H-%M-%S")
        self.path = SESSIONS_DIR / f"{stamp}_session.jsonl"
        self._fh = self.path.open("w")
        self._write({"type": "session_start", "t": time.time(), **(meta or {})})

    def _write(self, obj: dict[str, Any]) -> None:
        self._fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self._fh.flush()

    def log_event(self, ev: InteractionEvent) -> None:
        self._write({"type": "interaction", **asdict(ev)})

    def log_snapshot(
        self, t: float, frame: int, detections: list[dict], hands: list[dict]
    ) -> None:
        self._write(
            {"type": "snapshot", "t": t, "frame": frame,
             "detections": detections, "hands": hands}
        )

    def close(self) -> None:
        self._write({"type": "session_end", "t": time.time()})
        self._fh.close()
