"""Transport-neutral realtime provider contract and Qwen implementation."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import contextlib
import json
import os
from typing import Any, AsyncIterator, Literal, Protocol

import websockets


ProviderEventType = Literal[
    "ready",
    "speech_started",
    "speech_stopped",
    "assistant_text",
    "audio_started",
    "audio_pcm",
    "audio_done",
    "error",
]


@dataclass(frozen=True)
class ProviderEvent:
    type: ProviderEventType
    text: str | None = None
    pcm: bytes | None = None
    error_code: str | None = None
    error_message: str | None = None


class RealtimeProvider(Protocol):
    async def start(self, *, semantic_vad: bool, context: dict[str, Any]) -> None: ...

    async def send_audio(self, pcm16: bytes) -> None: ...

    async def update_context(self, revision: int, context: dict[str, Any]) -> None: ...

    async def announce(self, payload: dict[str, Any]) -> None: ...

    async def cancel_response(self) -> None: ...

    async def pause(self) -> None: ...

    async def resume(self) -> None: ...

    async def stop(self) -> None: ...

    def events(self) -> AsyncIterator[ProviderEvent]: ...


class QwenRealtimeProvider:
    """Qwen WebSocket provider with no local audio-device or file ownership."""

    def __init__(
        self,
        *,
        api_key: str,
        workspace_id: str,
        model: str = "qwen3.5-omni-flash-realtime",
        url: str | None = None,
    ) -> None:
        if not api_key or not workspace_id:
            raise ValueError("Qwen realtime configuration is incomplete")
        self._api_key = api_key
        self._workspace_id = workspace_id
        self._model = model
        self._url = url or (
            f"wss://{workspace_id}.cn-beijing.maas.aliyuncs.com"
            f"/api-ws/v1/realtime?model={model}"
        )
        self._ws: Any | None = None
        self._events: asyncio.Queue[ProviderEvent] = asyncio.Queue()
        self._reader: asyncio.Task[None] | None = None
        self._paused = False
        self._stopped = False

    @classmethod
    def from_environment(cls) -> "QwenRealtimeProvider":
        return cls(
            api_key=os.environ["QWEN_REALTIME_API_KEY"],
            workspace_id=os.environ["QWEN_REALTIME_WORKSPACE_ID"],
            model=os.environ.get(
                "QWEN_REALTIME_MODEL", "qwen3.5-omni-flash-realtime"
            ),
        )

    async def start(self, *, semantic_vad: bool, context: dict[str, Any]) -> None:
        self._ws = await websockets.connect(
            self._url,
            additional_headers={"Authorization": f"Bearer {self._api_key}"},
            close_timeout=2,
        )
        self._stopped = False
        self._reader = asyncio.create_task(self._read_loop(), name="qwen-realtime-reader")
        await self._send(
            {
                "type": "session.update",
                "session": {
                    "modalities": ["text", "audio"],
                    "input_audio_format": "pcm",
                    "output_audio_format": "pcm",
                    "turn_detection": {
                        "type": "semantic_vad" if semantic_vad else "server_vad",
                        "threshold": 0.5,
                        "silence_duration_ms": 800,
                    },
                    "instructions": json.dumps(context, ensure_ascii=False),
                },
            }
        )
        await self._events.put(ProviderEvent("ready"))

    async def send_audio(self, pcm16: bytes) -> None:
        if self._paused or self._stopped or self._ws is None:
            return
        await self._send(
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm16).decode("ascii"),
            }
        )

    async def update_context(self, revision: int, context: dict[str, Any]) -> None:
        if self._stopped or self._ws is None:
            return
        await self._send(
            {
                "type": "session.update",
                "session": {
                    "instructions": json.dumps(
                        {"context_revision": revision, "context": context},
                        ensure_ascii=False,
                    )
                },
            }
        )

    async def announce(self, payload: dict[str, Any]) -> None:
        if self._stopped or self._ws is None:
            raise RuntimeError("provider is not running")
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": payload["text"]}],
                },
            }
        )
        await self._send({"type": "response.create"})

    async def cancel_response(self) -> None:
        if self._ws is not None and not self._stopped:
            await self._send({"type": "response.cancel"})

    async def pause(self) -> None:
        self._paused = True

    async def resume(self) -> None:
        self._paused = False

    async def stop(self) -> None:
        self._stopped = True
        self._paused = True
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def events(self) -> AsyncIterator[ProviderEvent]:
        while not self._stopped or not self._events.empty():
            yield await self._events.get()

    async def _send(self, message: dict[str, Any]) -> None:
        if self._ws is None:
            raise RuntimeError("provider is not connected")
        await self._ws.send(json.dumps(message, ensure_ascii=False))

    async def _read_loop(self) -> None:
        if self._ws is None:
            return
        try:
            async for raw in self._ws:
                message = json.loads(raw)
                event_type = message.get("type")
                if event_type == "input_audio_buffer.speech_started":
                    await self._events.put(ProviderEvent("speech_started"))
                elif event_type == "input_audio_buffer.speech_stopped":
                    await self._events.put(ProviderEvent("speech_stopped"))
                elif event_type == "response.created":
                    await self._events.put(ProviderEvent("audio_started"))
                elif event_type == "response.audio_transcript.done":
                    await self._events.put(
                        ProviderEvent("assistant_text", text=message.get("transcript", ""))
                    )
                elif event_type == "response.audio.delta":
                    raw_audio = message.get("delta", "")
                    if raw_audio:
                        await self._events.put(
                            ProviderEvent("audio_pcm", pcm=base64.b64decode(raw_audio))
                        )
                elif event_type == "response.done":
                    await self._events.put(ProviderEvent("audio_done"))
                elif event_type == "error":
                    error = message.get("error") or {}
                    await self._events.put(
                        ProviderEvent(
                            "error",
                            error_code="MODEL_UNAVAILABLE",
                            error_message="provider error",
                        )
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._events.put(
                ProviderEvent(
                    "error",
                    error_code="AI_REALTIME_ERROR",
                    error_message="provider stream failed",
                )
            )
