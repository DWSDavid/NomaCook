"""Provider-neutral conversion from Qwen chunks to contract NDJSON."""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

from .contracts import ModelRequest
from .errors import ModelServiceError, safe_error
from .events import ModelEventStream
from .qwen_transport import ProviderChunk, QwenTransportError
from .tool_validation import ToolArgumentsError, validate_tool_arguments


class AgentModelService:
    def __init__(self, transport) -> None:
        self.transport = transport

    async def stream(self, request: ModelRequest) -> AsyncIterator[bytes]:
        events = ModelEventStream(
            request_id=request.request_id,
            turn_id=request.turn_id,
            provider_call_id=request.provider_call_id,
        )
        yield events.emit("response.accepted", {})
        yield events.emit("message.start", {})

        tool_name: str | None = None
        tool_arguments: list[str] = []
        stop_reason: str | None = None
        try:
            async for chunk in self.transport.stream(request):
                if stop_reason is not None:
                    yield events.fail(self._invalid_error())
                    return
                if chunk.kind == "text":
                    yield events.emit("text.delta", {"text": chunk.text or ""})
                elif chunk.kind == "usage":
                    yield events.emit("usage", chunk.usage or {})
                elif chunk.kind == "tool_name":
                    if tool_name is not None and tool_name != chunk.tool_name:
                        yield events.fail(self._invalid_error())
                        return
                    tool_name = chunk.tool_name
                elif chunk.kind == "tool_arguments":
                    if tool_name is None:
                        yield events.fail(self._invalid_error())
                        return
                    tool_arguments.append(chunk.tool_arguments or "")
                    if sum(len(part.encode("utf-8")) for part in tool_arguments) > 32 * 1024:
                        yield events.fail(self._invalid_error())
                        return
                elif chunk.kind == "stop":
                    stop_reason = chunk.finish_reason
                else:
                    yield events.fail(self._invalid_error())
                    return

            if stop_reason is None:
                yield events.fail(self._invalid_error())
                return
            if tool_name is not None:
                if stop_reason != "tool_call" or not tool_arguments:
                    yield events.fail(self._invalid_error())
                    return
                try:
                    arguments = validate_tool_arguments(
                        tool_name=tool_name,
                        arguments_json="".join(tool_arguments),
                        tools=request.tools,
                    )
                except ToolArgumentsError:
                    yield events.fail(self._invalid_error())
                    return
                yield events.emit(
                    "tool.call",
                    {"name": tool_name, "arguments": arguments},
                )
            elif stop_reason == "tool_call":
                yield events.fail(self._invalid_error())
                return
            terminal = events.end({"stop_reason": stop_reason})
            if terminal is not None:
                yield terminal
        except asyncio.CancelledError:
            cancelled = events.cancel(
                ModelServiceError(
                    code="REQUEST_CANCELLED",
                    retryable=False,
                    phase="provider_stream",
                    message="request cancelled",
                )
            )
            if cancelled is not None:
                yield cancelled
        except QwenTransportError as exc:
            failed = events.fail(exc.error)
            if failed is not None:
                yield failed
        except Exception as exc:
            failed = events.fail(safe_error(exc))
            if failed is not None:
                yield failed

    @staticmethod
    def _invalid_error() -> ModelServiceError:
        return ModelServiceError(
            code="MODEL_RESPONSE_INVALID",
            retryable=False,
            phase="provider_stream",
            message="model response invalid",
        )
