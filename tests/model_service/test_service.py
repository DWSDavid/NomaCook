from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

import pytest

from server.gateway.contracts import ModelEvent, ModelRequest
from server.gateway.errors import ModelServiceError
from server.gateway.qwen_transport import ProviderChunk, QwenTransportError
from server.gateway.service import AgentModelService


REPO = Path(__file__).resolve().parents[2]


def _request() -> ModelRequest:
    return ModelRequest.model_validate_json(
        (REPO / "server/gateway/contract/golden/request.json").read_text()
    )


class FakeTransport:
    def __init__(self, chunks: list[ProviderChunk | BaseException]) -> None:
        self.chunks = chunks
        self.calls = 0

    async def stream(self, request: ModelRequest) -> AsyncIterator[ProviderChunk]:
        self.calls += 1
        for chunk in self.chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            yield chunk


def _collect(service: AgentModelService, request: ModelRequest) -> list[ModelEvent]:
    async def run() -> list[ModelEvent]:
        return [
            ModelEvent.model_validate_json(line)
            async for line in service.stream(request)
        ]

    return asyncio.run(run())


def test_text_stop_and_usage_produce_complete_stream() -> None:
    transport = FakeTransport(
        [
            ProviderChunk(kind="text", text="hello"),
            ProviderChunk(kind="usage", usage={"total_tokens": 12}),
            ProviderChunk(kind="stop", finish_reason="stop"),
        ]
    )
    events = _collect(AgentModelService(transport), _request())
    assert [event.event_type for event in events] == [
        "response.accepted",
        "message.start",
        "text.delta",
        "usage",
        "message.end",
    ]
    assert events[-1].data["stop_reason"] == "stop"
    assert transport.calls == 1


def test_complete_allowed_tool_call_is_emitted_once() -> None:
    transport = FakeTransport(
        [
            ProviderChunk(kind="tool_name", tool_index=0, tool_name="nomacook.speak@1"),
            ProviderChunk(kind="tool_arguments", tool_index=0, tool_arguments='{"text":"hello"}'),
            ProviderChunk(kind="stop", finish_reason="tool_call"),
        ]
    )
    events = _collect(AgentModelService(transport), _request())
    assert [event.event_type for event in events] == [
        "response.accepted",
        "message.start",
        "tool.call",
        "message.end",
    ]
    assert events[2].data == {
        "name": "nomacook.speak@1",
        "arguments": {"text": "hello"},
    }


@pytest.mark.parametrize(
    "chunks",
    [
        [
            ProviderChunk(kind="tool_name", tool_index=0, tool_name="nomacook.speak@1"),
            ProviderChunk(kind="tool_arguments", tool_index=0, tool_arguments="{bad"),
            ProviderChunk(kind="stop", finish_reason="tool_call"),
        ],
        [
            ProviderChunk(kind="tool_name", tool_index=0, tool_name="nomacook.speak@1"),
            ProviderChunk(kind="tool_arguments", tool_index=0, tool_arguments='{"text":"ok","extra":true}'),
            ProviderChunk(kind="stop", finish_reason="tool_call"),
        ],
        [
            ProviderChunk(kind="tool_name", tool_index=0, tool_name="unknown.tool@1"),
            ProviderChunk(kind="tool_arguments", tool_index=0, tool_arguments='{"text":"ok"}'),
            ProviderChunk(kind="stop", finish_reason="tool_call"),
        ],
    ],
)
def test_invalid_tool_call_produces_failure_without_tool_event(
    chunks: list[ProviderChunk],
) -> None:
    events = _collect(AgentModelService(FakeTransport(chunks)), _request())
    assert events[-1].event_type == "response.failed"
    assert events[-1].error is not None
    assert events[-1].error.code == "MODEL_RESPONSE_INVALID"
    assert all(event.event_type != "tool.call" for event in events)


def test_multiple_tool_names_are_rejected() -> None:
    transport = FakeTransport(
        [
            ProviderChunk(kind="tool_name", tool_index=0, tool_name="nomacook.speak@1"),
            ProviderChunk(kind="tool_name", tool_index=1, tool_name="nomacook.submit_decision@1"),
        ]
    )
    events = _collect(AgentModelService(transport), _request())
    assert events[-1].event_type == "response.failed"
    assert all(event.event_type != "tool.call" for event in events)


@pytest.mark.parametrize(
    "error",
    [
        ModelServiceError("MODEL_TIMEOUT", True, "provider_stream", "timeout"),
        ModelServiceError("MODEL_RATE_LIMITED", True, "provider_connect", "rate limited"),
    ],
)
def test_provider_failures_are_safe_terminal_events(error: ModelServiceError) -> None:
    events = _collect(
        AgentModelService(FakeTransport([QwenTransportError(error)])), _request()
    )
    assert events[-1].event_type == "response.failed"
    assert events[-1].error is not None
    assert events[-1].error.code == error.code


def test_content_filter_ends_without_leaking_text() -> None:
    transport = FakeTransport(
        [
            ProviderChunk(kind="stop", finish_reason="content_filter"),
        ]
    )
    events = _collect(AgentModelService(transport), _request())
    assert events[-1].event_type == "message.end"
    assert events[-1].data["stop_reason"] == "content_filter"
    assert all(event.event_type != "text.delta" for event in events)


def test_cancel_during_partial_tool_discards_arguments() -> None:
    transport = FakeTransport(
        [
            ProviderChunk(kind="tool_name", tool_index=0, tool_name="nomacook.speak@1"),
            ProviderChunk(kind="tool_arguments", tool_index=0, tool_arguments='{"text":"partial"'),
            asyncio.CancelledError(),
        ]
    )
    events = _collect(AgentModelService(transport), _request())
    assert events[-1].event_type == "response.cancelled"
    assert all(event.event_type != "tool.call" for event in events)
