from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from server.gateway.contracts import ModelRequest
from server.gateway.qwen_transport import (
    ProviderChunk,
    QwenAgentConfig,
    QwenAgentTransport,
    QwenTransportError,
)


REPO = Path(__file__).resolve().parents[2]


def _request() -> ModelRequest:
    return ModelRequest.model_validate_json(
        (REPO / "server/gateway/contract/golden/request.json").read_text()
    )


def _request_with_both_tools() -> ModelRequest:
    payload = json.loads(
        (REPO / "server/gateway/contract/golden/request.json").read_text()
    )
    payload["tools"].append(
        {
            "name": "nomacook.submit_decision@1",
            "description": "Submit a bounded decision.",
            "parameters": {
                "type": "object",
                "properties": {"decision": {"type": "string"}},
                "required": ["decision"],
                "additionalProperties": False,
            },
        }
    )
    return ModelRequest.model_validate_json(json.dumps(payload))


def _sse(*payloads: dict[str, Any], done: bool = True) -> bytes:
    lines = [f"data: {json.dumps(payload, ensure_ascii=False)}\n\n" for payload in payloads]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def _transport(handler) -> tuple[QwenAgentTransport, dict[str, Any]]:
    state: dict[str, Any] = {"calls": 0, "request": None}

    async def wrapped(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        state["request"] = json.loads((await request.aread()).decode())
        return await handler(request, state)

    client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    return QwenAgentTransport(
        QwenAgentConfig(api_key="fake-key", workspace_id="fake-workspace"),
        client=client,
    ), state


def _run(transport: QwenAgentTransport) -> list[ProviderChunk]:
    async def collect() -> list[ProviderChunk]:
        return [chunk async for chunk in transport.stream(_request())]

    return asyncio.run(collect())


def test_text_usage_done_and_fixed_provider_profile() -> None:
    async def handler(request: httpx.Request, state: dict[str, Any]) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {"choices": [{"delta": {"content": "hello"}}]},
                {"choices": [{"finish_reason": "stop", "delta": {}}]},
                {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}},
            ),
            request=request,
        )

    transport, state = _transport(handler)
    chunks = _run(transport)
    assert [chunk.kind for chunk in chunks] == ["text", "usage", "stop"]
    assert chunks[0].text == "hello"
    assert state["calls"] == 1
    body = state["request"]
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["enable_thinking"] is False
    assert body["n"] == 1
    assert body["tool_stream"] is False
    assert "search" not in body
    assert "response_format" not in body


def test_one_tool_index_buffers_name_and_argument_fragments() -> None:
    async def handler(request: httpx.Request, state: dict[str, Any]) -> httpx.Response:
        return httpx.Response(
            200,
            content=_sse(
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "nomacook_speak_v1"}}]}}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"text":"hi"}'}}]}}]},
                {"choices": [{"finish_reason": "tool_calls", "delta": {}}]},
                {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}},
            ),
            request=request,
        )

    transport, _ = _transport(handler)
    chunks = _run(transport)
    assert [chunk.kind for chunk in chunks] == ["tool_name", "tool_arguments", "usage", "stop"]
    assert chunks[0].tool_name == "nomacook.speak@1"
    assert chunks[1].tool_arguments == '{"text":"hi"}'


def test_payload_uses_provider_safe_aliases_for_both_contract_tools() -> None:
    async def handler(request: httpx.Request, state: dict[str, Any]) -> httpx.Response:
        state["request"] = json.loads((await request.aread()).decode())
        return httpx.Response(
            200,
            content=_sse(
                {"choices": [{"finish_reason": "stop", "delta": {}}]},
                {"choices": [], "usage": {"total_tokens": 1}},
            ),
            request=request,
        )

    transport, state = _transport(handler)

    async def run() -> list[ProviderChunk]:
        return [chunk async for chunk in transport.stream(_request_with_both_tools())]

    asyncio.run(run())
    assert [tool["function"]["name"] for tool in state["request"]["tools"]] == [
        "nomacook_speak_v1",
        "nomacook_submit_decision_v1",
    ]


def test_unknown_provider_tool_alias_is_fail_closed() -> None:
    async def handler(request: httpx.Request, state: dict[str, Any]) -> httpx.Response:
        return httpx.Response(
            200,
            content=_sse(
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "unknown_tool_v1"}}]}}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"text":"hi"}'}}]}}]},
                {"choices": [{"finish_reason": "tool_calls", "delta": {}}]},
                {"choices": [], "usage": {"total_tokens": 1}},
            ),
            request=request,
        )

    transport, _ = _transport(handler)
    with pytest.raises(QwenTransportError) as exc:
        _run(transport)
    assert exc.value.error.code == "MODEL_RESPONSE_INVALID"


def test_multiple_finish_chunks_are_invalid() -> None:
    async def handler(request: httpx.Request, state: dict[str, Any]) -> httpx.Response:
        return httpx.Response(
            200,
            content=_sse(
                {"choices": [{"finish_reason": "stop", "delta": {}}]},
                {"choices": [{"finish_reason": "stop", "delta": {}}]},
            ),
            request=request,
        )

    transport, _ = _transport(handler)
    with pytest.raises(QwenTransportError) as exc:
        _run(transport)
    assert exc.value.error.code == "MODEL_RESPONSE_INVALID"


@pytest.mark.parametrize(
    "delta",
    [{"content": "late"}, {"tool_calls": [{"index": 0, "function": {"name": "nomacook_speak_v1"}}]}],
)
def test_provider_content_after_finish_is_invalid(delta: dict[str, Any]) -> None:
    async def handler(request: httpx.Request, state: dict[str, Any]) -> httpx.Response:
        return httpx.Response(
            200,
            content=_sse(
                {"choices": [{"finish_reason": "stop", "delta": {}}]},
                {"choices": [{"delta": delta}]},
            ),
            request=request,
        )

    transport, _ = _transport(handler)
    with pytest.raises(QwenTransportError) as exc:
        _run(transport)
    assert exc.value.error.code == "MODEL_RESPONSE_INVALID"


def test_two_tool_indexes_are_invalid() -> None:
    async def handler(request: httpx.Request, state: dict[str, Any]) -> httpx.Response:
        return httpx.Response(
            200,
            content=_sse({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"name": "nomacook.speak@1"}},
                {"index": 1, "function": {"name": "nomacook.submit_decision@1"}},
            ]}}]}),
            request=request,
        )

    transport, _ = _transport(handler)
    with pytest.raises(QwenTransportError) as exc:
        _run(transport)
    assert exc.value.error.code == "MODEL_RESPONSE_INVALID"


def test_reasoning_content_is_ignored_and_never_becomes_text() -> None:
    async def handler(request: httpx.Request, state: dict[str, Any]) -> httpx.Response:
        return httpx.Response(
            200,
            content=_sse(
                {"choices": [{"delta": {"reasoning_content": "private"}}]},
                {"choices": [{"delta": {"content": "visible"}}]},
                {"choices": [{"finish_reason": "stop", "delta": {}}]},
                {"choices": [], "usage": {"total_tokens": 1}},
            ),
            request=request,
        )

    transport, _ = _transport(handler)
    chunks = _run(transport)
    assert [chunk.text for chunk in chunks if chunk.kind == "text"] == ["visible"]


@pytest.mark.parametrize("body", [b"data: {not-json}\n\n", b"data: {}\n\n"])
def test_malformed_or_missing_done_is_invalid(body: bytes) -> None:
    async def handler(request: httpx.Request, state: dict[str, Any]) -> httpx.Response:
        return httpx.Response(200, content=body, request=request)

    transport, _ = _transport(handler)
    with pytest.raises(QwenTransportError) as exc:
        _run(transport)
    assert exc.value.error.code == "MODEL_RESPONSE_INVALID"


def test_rate_limit_is_bounded_and_does_not_retry() -> None:
    async def handler(request: httpx.Request, state: dict[str, Any]) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "120"}, content=b"private", request=request)

    transport, state = _transport(handler)
    with pytest.raises(QwenTransportError) as exc:
        _run(transport)
    assert exc.value.error.code == "MODEL_RATE_LIMITED"
    assert exc.value.error.retry_after_ms == 60000
    assert state["calls"] == 1


@pytest.mark.parametrize("status", [401, 403, 500, 502])
def test_http_failure_is_safe_and_single_attempt(status: int) -> None:
    async def handler(request: httpx.Request, state: dict[str, Any]) -> httpx.Response:
        return httpx.Response(status, content=b"provider secret body", request=request)

    transport, state = _transport(handler)
    with pytest.raises(QwenTransportError) as exc:
        _run(transport)
    assert exc.value.error.code in {"MODEL_UNAVAILABLE", "AI_MODEL_SERVICE_ERROR"}
    assert "provider secret body" not in str(exc.value)
    assert state["calls"] == 1


def test_timeout_is_safe_and_single_attempt() -> None:
    async def handler(request: httpx.Request, state: dict[str, Any]) -> httpx.Response:
        raise httpx.ReadTimeout("private timeout detail", request=request)

    transport, state = _transport(handler)
    with pytest.raises(QwenTransportError) as exc:
        _run(transport)
    assert exc.value.error.code == "MODEL_TIMEOUT"
    assert state["calls"] == 1


class _BlockingStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = asyncio.Event()
        self.release = asyncio.Event()

    async def __aiter__(self):
        self.started.set()
        try:
            await self.release.wait()
            yield b"data: {}\n\n"
        finally:
            self.closed.set()


def test_task_cancellation_closes_http_stream_without_late_chunks() -> None:
    state: dict[str, Any] = {"stream": _BlockingStream(), "calls": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        return httpx.Response(200, content=state["stream"], request=request)

    async def run() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        transport = QwenAgentTransport(
            QwenAgentConfig(api_key="fake-key", workspace_id="fake-workspace"),
            client=client,
        )
        async def collect() -> None:
            async for _ in transport.stream(_request()):
                pass

        task = asyncio.create_task(collect())
        await state["stream"].started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(state["stream"].closed.wait(), timeout=1)
        assert state["calls"] == 1

    asyncio.run(run())
