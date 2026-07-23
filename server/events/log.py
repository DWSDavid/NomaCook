"""Idempotent append-only JSONL persistence for event envelopes."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import threading
from typing import Any, Iterator

from pydantic import ValidationError

from .schema import EventEnvelope


class EventLogError(RuntimeError):
    """Base event-log failure."""


class EventLogCorruption(EventLogError):
    """An existing JSONL row cannot be trusted or replayed."""


class DuplicateEventConflict(EventLogError):
    """A stable event_id was reused for different event content."""


def _canonical_json(event: EventEnvelope, *, include_received_at: bool) -> str:
    return json.dumps(
        event.canonical_dict(include_received_at=include_received_at),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _fingerprint(event: EventEnvelope) -> str:
    content = _canonical_json(event, include_received_at=False).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def read_events(path: str | Path) -> list[EventEnvelope]:
    """Read and strictly validate every non-empty JSONL row."""

    event_path = Path(path)
    if not event_path.exists():
        return []

    events: list[EventEnvelope] = []
    seen_ids: set[str] = set()
    with event_path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                event = EventEnvelope.model_validate_json(line)
            except (ValidationError, ValueError) as exc:
                raise EventLogCorruption(
                    f"{event_path}:{line_number}: invalid event envelope: {exc}"
                ) from exc
            if event.event_id in seen_ids:
                raise EventLogCorruption(
                    f"{event_path}:{line_number}: duplicate event_id {event.event_id!r}"
                )
            seen_ids.add(event.event_id)
            events.append(event)
    return events


class EventLog:
    """A single-process writer with retry deduplication and conflict detection."""

    def __init__(self, path: str | Path, *, durable: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._durable = durable
        self._lock = threading.Lock()
        self._fingerprints = {
            event.event_id: _fingerprint(event) for event in read_events(self.path)
        }

    def append(self, event: EventEnvelope | dict[str, Any]) -> bool:
        """Append once; return False for an identical retry with the same event_id."""

        envelope = (
            event if isinstance(event, EventEnvelope) else EventEnvelope.model_validate(event)
        )
        fingerprint = _fingerprint(envelope)

        with self._lock:
            existing = self._fingerprints.get(envelope.event_id)
            if existing is not None:
                if existing == fingerprint:
                    return False
                raise DuplicateEventConflict(
                    f"event_id {envelope.event_id!r} already has different content"
                )

            line = envelope.model_dump_json() + "\n"
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(line)
                stream.flush()
                if self._durable:
                    os.fsync(stream.fileno())
            self._fingerprints[envelope.event_id] = fingerprint
            return True

    def __len__(self) -> int:
        return len(self._fingerprints)

    def events(self, *, order_by_seq: bool = False) -> Iterator[EventEnvelope]:
        events = read_events(self.path)
        if order_by_seq:
            events.sort(key=lambda event: (event.seq, event.event_id))
        return iter(events)
