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
MAX_DIALOGUE_TURNS = 6


class HotMemory:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot: TaskSnapshot | None = None
        self._recent_events: list[dict[str, Any]] = []
        self._latest_transition: dict[str, Any] | None = None
        self._pending_question: str | None = None
        self._context_version: int = 0
        self._last_event_seq: int = -1
        self._recent_dialogue: list[dict[str, Any]] = []

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

    def add_dialogue_turn(
        self,
        *,
        user_transcript: str,
        assistant_transcript: str,
        response_was_grounded: bool,
        session_dir: Path | None = None,
    ) -> None:
        with self._lock:
            state = self._snapshot.state if self._snapshot else "unknown"
            turn = {
                "user": user_transcript,
                "assistant": assistant_transcript,
                "state_at_turn": state,
                "timestamp": __import__("time").time(),
                "response_was_grounded": response_was_grounded,
            }
            self._recent_dialogue.append(turn)
            self._recent_dialogue = self._recent_dialogue[-MAX_DIALOGUE_TURNS:]
        if session_dir:
            with (session_dir / "dialogue_events.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(turn, ensure_ascii=False) + "\n")

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
                "recent_dialogue": list(self._recent_dialogue),
            }

    def snapshot(self) -> TaskSnapshot | None:
        with self._lock:
            return self._snapshot

    def compact_context(self) -> str:
        """One compact paragraph for Qwen system context."""
        with self._lock:
            snap = self._snapshot
            if snap is None:
                return ("task_goal: unknown\ncurrent_step_id: unknown\n"
                        "current_step_title: unknown\ncurrent_instruction: unknown\n"
                        "status: ON_TRACK\npending_question: null")

            lines = [
                f"task_goal: {snap.task_goal}",
                f"current_step_id: {snap.state}",
                f"current_step_title: {snap.step_title}",
                f"current_instruction: {snap.step_instruction}",
                f"status: {snap.status}",
                f"pending_question: {snap.pending_question}",
            ]
            if snap.active_objects:
                lines.append(f"active_objects: {', '.join(snap.active_objects)}")
            if snap.missing_evidence:
                lines.append(f"missing_evidence: {', '.join(snap.missing_evidence)}")

            if self._recent_dialogue:
                lines.append("recent_dialogue:")
                for d in self._recent_dialogue[-3:]:
                    lines.append(f"  user: {d['user']}")
                    lines.append(f"  assistant: {d['assistant']}")

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
