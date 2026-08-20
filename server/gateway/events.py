"""Bounded NDJSON event stream with one terminal owner."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from .contracts import (
    CONTRACT_VERSION,
    SCHEMA_VERSION,
    ModelEvent,
    TERMINAL_EVENT_TYPES,
)
from .errors import ModelServiceError


MAX_EVENT_LINE_BYTES = 64 * 1024
MAX_EVENT_COUNT = 512
HEARTBEAT_INTERVAL = timedelta(seconds=5)


class ModelEventStream:
    def __init__(
        self,
        *,
        request_id: str,
        turn_id: str,
        provider_call_id: str,
        occurred_at: datetime | None = None,
        max_events: int = MAX_EVENT_COUNT,
    ) -> None:
        self.request_id = request_id
        self.turn_id = turn_id
        self.provider_call_id = provider_call_id
        self._occurred_at = occurred_at or datetime.now(UTC)
        self._max_events = min(max_events, MAX_EVENT_COUNT)
        if self._max_events < 1:
            raise ValueError("max_events must be positive")
        self._sequence = 0
        self._count = 0
        self._closed = False
        self._last_heartbeat_at = self._occurred_at

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def count(self) -> int:
        return self._count

    def emit(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        error: ModelServiceError | None = None,
        occurred_at: datetime | None = None,
    ) -> bytes:
        if self._closed:
            raise RuntimeError("event stream is closed")
        if self._count >= self._max_events:
            raise RuntimeError("event stream event limit exceeded")
        when = occurred_at or self._occurred_at
        self._sequence += 1
        event = ModelEvent(
            contract_version=CONTRACT_VERSION,
            schema_version=SCHEMA_VERSION,
            request_id=self.request_id,
            turn_id=self.turn_id,
            provider_call_id=self.provider_call_id,
            stream_sequence=self._sequence,
            event_type=event_type,
            occurred_at=when,
            data=data,
            error=error.to_model_error() if error else None,
        )
        line = (event.model_dump_json() + "\n").encode("utf-8")
        if len(line) > MAX_EVENT_LINE_BYTES:
            self._sequence -= 1
            raise ValueError("event line exceeds 64 KiB")
        self._count += 1
        self._occurred_at = when
        if event_type == "response.heartbeat":
            self._last_heartbeat_at = when
        if event_type in TERMINAL_EVENT_TYPES:
            self._closed = True
        return line

    def heartbeat(self, *, now: datetime | None = None) -> bytes | None:
        if self._closed:
            return None
        current = now or datetime.now(UTC)
        if current - self._last_heartbeat_at < HEARTBEAT_INTERVAL:
            return None
        return self.emit("response.heartbeat", {}, occurred_at=current)

    def end(self, data: dict[str, Any]) -> bytes | None:
        if self._closed:
            return None
        return self.emit("message.end", data)

    def fail(self, error: ModelServiceError) -> bytes | None:
        if self._closed:
            return None
        return self.emit("response.failed", {}, error=error)

    def cancel(self, error: ModelServiceError) -> bytes | None:
        if self._closed:
            return None
        return self.emit("response.cancelled", {}, error=error)

    def close(self) -> None:
        self._closed = True
