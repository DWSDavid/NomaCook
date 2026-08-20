from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json

import pytest

from server.gateway.errors import ModelServiceError, safe_error
from server.gateway.events import ModelEventStream
from server.gateway.registry import ProviderCallRegistry


def _stream(**kwargs) -> ModelEventStream:
    kwargs.setdefault("occurred_at", datetime(2026, 8, 20, 10, 0, tzinfo=UTC))
    return ModelEventStream(
        request_id="request_demo_01",
        turn_id="turn_demo_01",
        provider_call_id="provider_call_demo_01",
        **kwargs,
    )


def _event(line: bytes) -> dict:
    return json.loads(line)


def test_registry_admits_first_id_rejects_active_and_completed_duplicate() -> None:
    async def run() -> None:
        registry = ProviderCallRegistry(recent_capacity=2)
        assert await registry.admit("call_a") is True
        assert await registry.admit("call_a") is False
        await registry.complete("call_a")
        assert await registry.admit("call_a") is False

    asyncio.run(run())


def test_registry_evicts_oldest_completed_id_only_after_capacity() -> None:
    async def run() -> None:
        registry = ProviderCallRegistry(recent_capacity=2)
        for call_id in ("call_a", "call_b"):
            assert await registry.admit(call_id) is True
            await registry.complete(call_id)

        assert await registry.admit("call_c") is True
        await registry.complete("call_c")
        assert await registry.admit("call_a") is True

    asyncio.run(run())


def test_event_sequence_starts_at_one_and_terminal_is_unique() -> None:
    stream = _stream()
    first = _event(stream.emit("response.accepted", {}))
    second = _event(stream.emit("message.start", {}))
    terminal = _event(stream.end({"stop_reason": "stop"}))
    assert [first["stream_sequence"], second["stream_sequence"], terminal["stream_sequence"]] == [1, 2, 3]
    assert terminal["event_type"] == "message.end"
    assert stream.end({"stop_reason": "stop"}) is None
    with pytest.raises(RuntimeError):
        stream.emit("text.delta", {"text": "late"})


def test_event_line_and_event_count_limits() -> None:
    stream = _stream(max_events=1)
    stream.emit("response.accepted", {})
    with pytest.raises(RuntimeError):
        stream.emit("response.heartbeat", {})

    huge = _stream()
    with pytest.raises(ValueError):
        huge.emit("text.delta", {"text": "x" * (8 * 1024 + 1)})


def test_heartbeat_is_emitted_after_five_seconds() -> None:
    now = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
    stream = _stream(occurred_at=now)
    assert stream.heartbeat(now=now + timedelta(seconds=4)) is None
    line = stream.heartbeat(now=now + timedelta(seconds=5))
    assert line is not None
    assert _event(line)["event_type"] == "response.heartbeat"


def test_error_and_cancel_are_terminal_and_safe() -> None:
    stream = _stream()
    error = ModelServiceError(
        code="MODEL_TIMEOUT", retryable=True, phase="provider_stream", message="deadline exceeded"
    )
    failed = _event(stream.fail(error))
    assert failed["event_type"] == "response.failed"
    assert failed["error"]["code"] == "MODEL_TIMEOUT"

    cancelled = _stream().cancel(
        ModelServiceError(
            code="REQUEST_CANCELLED", retryable=False, phase="provider_stream", message="cancelled"
        )
    )
    assert _event(cancelled)["event_type"] == "response.cancelled"


def test_safe_error_does_not_expose_exception_text() -> None:
    error = safe_error(RuntimeError("api_key=secret-token prompt=private"))
    assert error.code == "AI_MODEL_SERVICE_ERROR"
    assert "secret-token" not in error.message
    assert "private" not in error.message
