"""Thread-safe bounded hot memory for downstream consumers (e.g. Qwen).

Ownership: the StateEngine loop writes; Qwen and session recorder read.
Never holds observations or raw frames. Never modifies StateEngine.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from server.engine.snapshot import TaskSnapshot

MAX_RECENT_EVENTS = 12


class HotMemory:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot: TaskSnapshot | None = None
        self._recent_events: list[dict[str, Any]] = []
        self._latest_transition: dict[str, Any] | None = None
        self._pending_question: str | None = None
        self._context_version: int = 0
        self._last_event_seq: int = -1

    def update(
        self,
        *,
        snapshot: TaskSnapshot | None = None,
        recent_events: list[dict[str, Any]] | None = None,
        latest_transition: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            if snapshot is not None:
                self._snapshot = snapshot
                self._context_version = snapshot.context_version
                self._last_event_seq = snapshot.last_event_seq
                self._pending_question = snapshot.pending_question
            if recent_events is not None:
                self._recent_events.extend(recent_events)
                self._recent_events = self._recent_events[-MAX_RECENT_EVENTS:]
            if latest_transition is not None:
                self._latest_transition = latest_transition

    def read(self) -> dict[str, Any]:
        with self._lock:
            snap = self._snapshot
            return {
                "snapshot": snap.model_dump() if snap else None,
                "recent_events": list(self._recent_events),
                "latest_transition": self._latest_transition,
                "pending_question": self._pending_question,
                "context_version": self._context_version,
                "last_event_seq": self._last_event_seq,
            }

    def snapshot(self) -> TaskSnapshot | None:
        with self._lock:
            return self._snapshot

    def compact_context(self) -> str:
        """One compact paragraph for Qwen system context."""
        with self._lock:
            snap = self._snapshot
            if snap is None:
                return "current_state: unknown\nstatus: ON_TRACK\nbelief: 0.00\nno recent events"

            lines = [
                f"current_state: {snap.state}",
                f"status: {snap.status}",
                f"belief: {snap.belief:.2f}",
            ]
            if snap.active_objects:
                lines.append(f"active_objects: {', '.join(snap.active_objects)}")
            if snap.missing_evidence:
                lines.append(f"missing_evidence: {', '.join(snap.missing_evidence)}")
            if snap.pending_question:
                lines.append(f"pending_question: {snap.pending_question}")

            if self._recent_events:
                recent_str = ", ".join(
                    e.get("type", "?") for e in self._recent_events[-6:]
                )
                lines.append(f"recent_events: {recent_str}")

            return "\n".join(lines)

    def write_latest_snapshot(self, session_dir: Path | None) -> None:
        """Atomically write latest_snapshot.json for debugging."""
        if session_dir is None:
            return
        with self._lock:
            snap = self._snapshot
        if snap is None:
            return
        tmp = session_dir / ".latest_snapshot.tmp"
        dst = session_dir / "latest_snapshot.json"
        tmp.write_text(json.dumps(snap.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(dst)
