"""One-shot cancellable Qwen-compatible streaming transport."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from typing import Any, AsyncIterator, Literal

import httpx

from .contracts import (
    INTERNAL_TO_PROVIDER_TOOL,
    PROVIDER_TO_INTERNAL_TOOL,
    ModelRequest,
)
from .errors import ModelServiceError


ProviderChunkKind = Literal["text", "tool_name", "tool_arguments", "usage", "stop"]
MAX_PROVIDER_LINE_BYTES = 64 * 1024
MAX_TOOL_ARGUMENT_BYTES = 32 * 1024


@dataclass(frozen=True)
class ProviderChunk:
    kind: ProviderChunkKind
    text: str | None = None
    tool_index: int | None = None
    tool_name: str | None = None
    tool_arguments: str | None = None
    usage: dict[str, int] | None = None
    finish_reason: str | None = None


@dataclass(frozen=True)
class QwenAgentConfig:
    api_key: str
    workspace_id: str
    model: str = "qwen3.6-flash"
    timeout_seconds: float = 30.0
    region: str = "cn-beijing"

    def __post_init__(self) -> None:
        if not self.api_key or not self.workspace_id or not self.model:
            raise ValueError("Qwen transport configuration is incomplete")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

    @property
    def endpoint(self) -> str:
        return (
            f"https://{self.workspace_id}.{self.region}.maas.aliyuncs.com"
            "/compatible-mode/v1/chat/completions"
        )


class QwenTransportError(RuntimeError):
    def __init__(self, error: ModelServiceError) -> None:
        super().__init__(error.message)
        self.error = error


class QwenAgentTransport:
    def __init__(
        self,
        config: QwenAgentConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._client = client or httpx.AsyncClient()

    async def stream(self, request: ModelRequest) -> AsyncIterator[ProviderChunk]:
        payload = self._payload(request)
        timeout_seconds = min(
            self.config.timeout_seconds,
            request.timeout_ms / 1000.0,
            (request.deadline_at - request.started_at).total_seconds(),
        )
        if timeout_seconds <= 0:
            raise QwenTransportError(
                ModelServiceError(
                    code="MODEL_TIMEOUT",
                    retryable=True,
                    phase="provider_stream",
                    message="model deadline exceeded",
                )
            )

        seen_done = False
        pending_finish_reason: str | None = None
        seen_usage_after_finish = False
        active_tool_index: int | None = None
        argument_bytes = 0
        try:
            async with self._client.stream(
                "POST",
                self.config.endpoint,
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
                json=payload,
                timeout=timeout_seconds,
            ) as response:
                if response.status_code == 429:
                    raise QwenTransportError(
                        ModelServiceError(
                            code="MODEL_RATE_LIMITED",
                            retryable=True,
                            phase="provider_connect",
                            message="model rate limit",
                            retry_after_ms=_retry_after_ms(response.headers.get("retry-after")),
                        )
                    )
                if response.status_code in {401, 403} or response.status_code >= 500:
                    raise QwenTransportError(
                        ModelServiceError(
                            code="MODEL_UNAVAILABLE",
                            retryable=response.status_code >= 500,
                            phase="provider_connect",
                            message="model provider unavailable",
                        )
                    )
                if response.status_code >= 400:
                    raise QwenTransportError(
                        ModelServiceError(
                            code="MODEL_UNAVAILABLE",
                            retryable=False,
                            phase="provider_connect",
                            message="model request rejected",
                        )
                    )

                async for raw_line in response.aiter_lines():
                    if len(raw_line.encode("utf-8")) > MAX_PROVIDER_LINE_BYTES:
                        raise self._invalid_response()
                    line = raw_line.strip()
                    if not line:
                        continue
                    if not line.startswith("data:"):
                        raise self._invalid_response()
                    data = line[5:].strip()
                    if data == "[DONE]":
                        seen_done = True
                        break
                    try:
                        item = json.loads(data)
                    except Exception as exc:
                        del exc
                        raise self._invalid_response()
                    if not isinstance(item, dict):
                        raise self._invalid_response()

                    choices = item.get("choices", [])
                    if not isinstance(choices, list):
                        raise self._invalid_response()
                    usage = item.get("usage")
                    if pending_finish_reason is not None:
                        # After a finish choice, only one choices=[] usage
                        # tail is legal before [DONE].
                        if choices or usage is None or seen_usage_after_finish:
                            raise self._invalid_response()
                        yield ProviderChunk(kind="usage", usage=_usage(usage))
                        seen_usage_after_finish = True
                        continue
                    if usage is not None:
                        raise self._invalid_response()

                    for choice in choices:
                        if not isinstance(choice, dict):
                            raise self._invalid_response()
                        delta = choice.get("delta") or {}
                        if not isinstance(delta, dict):
                            raise self._invalid_response()

                        finish_reason = choice.get("finish_reason")
                        if finish_reason is not None:
                            if finish_reason == "tool_calls":
                                finish_reason = "tool_call"
                            if finish_reason not in {
                                "stop",
                                "tool_call",
                                "length",
                                "content_filter",
                            }:
                                raise self._invalid_response()
                            if delta.get("content") or delta.get("tool_calls"):
                                raise self._invalid_response()
                            if pending_finish_reason is not None:
                                raise self._invalid_response()
                            pending_finish_reason = finish_reason
                            continue

                        content = delta.get("content")
                        if content is not None:
                            if not isinstance(content, str):
                                raise self._invalid_response()
                            if content:
                                yield ProviderChunk(kind="text", text=content)

                        tool_calls = delta.get("tool_calls") or []
                        if not isinstance(tool_calls, list):
                            raise self._invalid_response()
                        for tool_call in tool_calls:
                            if not isinstance(tool_call, dict):
                                raise self._invalid_response()
                            index = tool_call.get("index")
                            if not isinstance(index, int) or index < 0:
                                raise self._invalid_response()
                            if active_tool_index is None:
                                active_tool_index = index
                            elif active_tool_index != index:
                                raise self._invalid_response()
                            function = tool_call.get("function") or {}
                            if not isinstance(function, dict):
                                raise self._invalid_response()
                            name = function.get("name")
                            if name is not None:
                                if not isinstance(name, str) or not name:
                                    raise self._invalid_response()
                                internal_name = PROVIDER_TO_INTERNAL_TOOL.get(name)
                                if internal_name is None:
                                    raise self._invalid_response()
                                yield ProviderChunk(
                                    kind="tool_name",
                                    tool_index=index,
                                    tool_name=internal_name,
                                )
                            arguments = function.get("arguments")
                            if arguments is not None:
                                if not isinstance(arguments, str):
                                    raise self._invalid_response()
                                argument_bytes += len(arguments.encode("utf-8"))
                                if argument_bytes > MAX_TOOL_ARGUMENT_BYTES:
                                    raise self._invalid_response()
                                if arguments:
                                    yield ProviderChunk(
                                        kind="tool_arguments",
                                        tool_index=index,
                                        tool_arguments=arguments,
                                    )

                if not seen_done or pending_finish_reason is None or not seen_usage_after_finish:
                    raise self._invalid_response()
                yield ProviderChunk(kind="stop", finish_reason=pending_finish_reason)
        except asyncio.CancelledError:
            raise
        except QwenTransportError:
            raise
        except httpx.TimeoutException:
            raise QwenTransportError(
                ModelServiceError(
                    code="MODEL_TIMEOUT",
                    retryable=True,
                    phase="provider_stream",
                    message="model provider timeout",
                )
            )
        except httpx.HTTPError:
            raise QwenTransportError(
                ModelServiceError(
                    code="MODEL_UNAVAILABLE",
                    retryable=True,
                    phase="provider_stream",
                    message="model provider unavailable",
                )
            )

    @staticmethod
    def _invalid_response() -> QwenTransportError:
        return QwenTransportError(
            ModelServiceError(
                code="MODEL_RESPONSE_INVALID",
                retryable=False,
                phase="provider_stream",
                message="model response invalid",
            )
        )

    def _payload(self, request: ModelRequest) -> dict[str, Any]:
        return {
            "model": self.config.model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in request.messages
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": INTERNAL_TO_PROVIDER_TOOL[tool.name],
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in request.tools
            ],
            "max_tokens": request.options.max_output_tokens,
            "temperature": request.options.temperature,
            "tool_choice": request.options.tool_choice,
            "stream": True,
            "stream_options": {"include_usage": True},
            "enable_thinking": False,
            "n": 1,
            "tool_stream": False,
        }


def _usage(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        raise QwenTransportError(
            ModelServiceError(
                code="MODEL_RESPONSE_INVALID",
                retryable=False,
                phase="provider_stream",
                message="model usage invalid",
            )
        )
    result: dict[str, int] = {}
    for key, raw in value.items():
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise QwenTransportError(
                ModelServiceError(
                    code="MODEL_RESPONSE_INVALID",
                    retryable=False,
                    phase="provider_stream",
                    message="model usage invalid",
                )
            )
        result[str(key)] = raw
    return result


def _retry_after_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return min(60000, max(0, int(float(value) * 1000)))
    except (TypeError, ValueError):
        return None
