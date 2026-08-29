"""Authenticated internal WebSocket gateway for Realtime Model Service v1."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import os
import threading
from typing import Callable, Mapping

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .auth import verify_service_bearer
from .errors import ModelServiceError
from server.realtime.codec import OpusCodec
from server.realtime.contracts import (
    SUBPROTOCOL,
    RealtimeEnvelope,
    parse_control_json,
)
from server.realtime.provider import QwenRealtimeProvider, RealtimeProvider
from server.realtime.session import RealtimeSession, SessionError


@dataclass(frozen=True)
class RealtimeSettings:
    service_token: str
    provider_enabled: bool
    qwen_api_key: str
    qwen_workspace_id: str
    qwen_model: str
    max_concurrency: int
    host: str
    port: int

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> "RealtimeSettings":
        values = dict(os.environ if environment is None else environment)
        return cls(
            service_token=values.get("AI_REALTIME_SERVICE_TOKEN", ""),
            provider_enabled=values.get("AI_REALTIME_PROVIDER_ENABLED", "false").lower() == "true",
            qwen_api_key=values.get("QWEN_REALTIME_API_KEY", ""),
            qwen_workspace_id=values.get("QWEN_REALTIME_WORKSPACE_ID", ""),
            qwen_model=values.get("QWEN_REALTIME_MODEL", "qwen3.5-omni-flash-realtime"),
            max_concurrency=_int_env(values, "AI_REALTIME_MAX_CONCURRENCY", 2, 1, 128),
            host=values.get("AI_REALTIME_HOST", "127.0.0.1"),
            port=_int_env(values, "AI_REALTIME_PORT", 8092, 1, 65535),
        )

    @property
    def ready(self) -> bool:
        return (
            bool(self.service_token)
            and self.provider_enabled
            and bool(self.qwen_api_key)
            and bool(self.qwen_workspace_id)
            and self.qwen_model == "qwen3.5-omni-flash-realtime"
            and self.max_concurrency > 0
        )


def _int_env(values: Mapping[str, str], name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(values.get(name, default))
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, value))


class _OwnerRegistry:
    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._owners: dict[tuple[str, int], RealtimeSession] = {}
        self._lock = threading.Lock()

    def admit(self, key: tuple[str, int], session: RealtimeSession) -> bool:
        with self._lock:
            if key in self._owners or len(self._owners) >= self.capacity:
                return False
            self._owners[key] = session
            return True

    def remove(self, key: tuple[str, int]) -> None:
        with self._lock:
            self._owners.pop(key, None)

    @property
    def active(self) -> int:
        with self._lock:
            return len(self._owners)


def create_realtime_app(
    *,
    settings: RealtimeSettings,
    provider_factory: Callable[[], RealtimeProvider] | None = None,
    codec_factory: Callable[[], OpusCodec] = OpusCodec,
) -> FastAPI:
    app = FastAPI(title="NomaCook Realtime Model Service", version="1.0")
    app.state.settings = settings
    app.state.codec_ready = _codec_available(codec_factory)
    app.state.owners = _OwnerRegistry(settings.max_concurrency)

    if provider_factory is None:
        provider_factory = QwenRealtimeProvider.from_environment

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> JSONResponse:
        if not settings.ready or not app.state.codec_ready:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "MODEL_UNAVAILABLE",
                        "retryable": True,
                        "phase": "readiness",
                        "message": "realtime service unavailable",
                    }
                },
            )
        return JSONResponse(status_code=200, content={"status": "ready"})

    @app.websocket("/v1/realtime-sessions:stream")
    async def realtime_stream(websocket: WebSocket) -> None:
        if not verify_service_bearer(
            websocket.headers.get("authorization"), settings.service_token
        ):
            await websocket.close(code=4401)
            return
        if SUBPROTOCOL not in websocket.scope.get("subprotocols", []):
            await websocket.close(code=4406)
            return
        await websocket.accept(subprotocol=SUBPROTOCOL)

        session: RealtimeSession | None = None
        key: tuple[str, int] | None = None
        provider_task: asyncio.Task[None] | None = None
        sender_task: asyncio.Task[None] | None = None
        try:
            first = await asyncio.wait_for(websocket.receive(), timeout=3.0)
            first_text = first.get("text")
            if first_text is None:
                await websocket.close(code=4400)
                return
            envelope = parse_control_json(first_text)
            if envelope.producer != "backend" or envelope.message_type != "session.start":
                await websocket.close(code=4400)
                return
            key = (str(envelope.session_id), envelope.session_generation)
            provider = provider_factory()
            session = RealtimeSession(
                key[0],
                key[1],
                provider=provider,
                codec=codec_factory(),
            )
            if not app.state.owners.admit(key, session):
                await websocket.close(code=4409)
                return
            await session.handle_text(first_text)
            await _send_pending(websocket, session)

            async def send_loop() -> None:
                while not session.closed:
                    await session.wait_for_output()
                    await _send_pending(websocket, session)

            sender_task = asyncio.create_task(send_loop(), name="realtime-sender")
            provider_task = asyncio.create_task(
                session.run_provider_events(), name="realtime-provider-events"
            )
            while not session.closed:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                if message.get("text") is not None:
                    await session.handle_text(message["text"])
                elif message.get("bytes") is not None:
                    await session.handle_binary(message["bytes"])
        except (WebSocketDisconnect, asyncio.TimeoutError):
            pass
        except (SessionError, ValueError):
            if websocket.client_state.name == "CONNECTED":
                await websocket.close(code=4400)
        finally:
            if sender_task is not None:
                sender_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sender_task
            if provider_task is not None:
                provider_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await provider_task
            if session is not None:
                await session.close()
            if key is not None:
                app.state.owners.remove(key)

    return app


async def _send_pending(websocket: WebSocket, session: RealtimeSession) -> None:
    for event in await session.drain_events():
        await websocket.send_text(event.model_dump_json())
    for frame in await session.drain_audio():
        await websocket.send_bytes(frame)


def _codec_available(factory: Callable[[], OpusCodec]) -> bool:
    try:
        factory()
        return True
    except Exception:
        return False
