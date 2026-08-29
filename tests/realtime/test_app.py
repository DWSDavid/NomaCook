from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from fastapi.testclient import TestClient

from server.gateway.realtime_app import RealtimeSettings, create_realtime_app
from server.realtime.provider import ProviderEvent


SESSION_ID = "11111111-1111-1111-1111-111111111111"


class FakeProvider:
    def __init__(self) -> None:
        self.events_queue: asyncio.Queue[ProviderEvent] = asyncio.Queue()

    async def start(self, *, semantic_vad: bool, context: dict) -> None:
        self.semantic_vad = semantic_vad

    async def send_audio(self, pcm16: bytes) -> None:
        pass

    async def update_context(self, revision: int, context: dict) -> None:
        pass

    async def announce(self, payload: dict) -> None:
        pass

    async def cancel_response(self) -> None:
        pass

    async def pause(self) -> None:
        pass

    async def resume(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def events(self) -> AsyncIterator[ProviderEvent]:
        while True:
            yield await self.events_queue.get()


def _settings(**overrides) -> RealtimeSettings:
    values = {
        "service_token": "local",
        "provider_enabled": True,
        "qwen_api_key": "fake",
        "qwen_workspace_id": "workspace",
        "qwen_model": "qwen3.5-omni-flash-realtime",
        "max_concurrency": 2,
        "host": "127.0.0.1",
        "port": 8091,
    }
    values.update(overrides)
    return RealtimeSettings(**values)


def _start() -> dict:
    profile = {"encoding": "opus", "clock_rate_hz": 48000, "channels": 2, "frame_duration_ms": 20}
    return {
        "contract_version": "ai-realtime.contract.v1",
        "schema_version": "1.0",
        "session_id": SESSION_ID,
        "session_generation": 1,
        "producer": "backend",
        "stream_sequence": 1,
        "message_type": "session.start",
        "occurred_at": "2026-08-29T10:00:00Z",
        "payload": {
            "started_at": "2026-08-29T10:00:00Z",
            "deadline_at": "2026-08-29T11:00:00Z",
            "input_audio": profile,
            "output_audio": profile,
            "context_revision": 1,
            "context": {},
            "limits": {"max_binary_frame_bytes": 65536, "max_buffered_audio_ms": 1000},
        },
    }


def test_health_ready_and_authenticated_websocket() -> None:
    provider = FakeProvider()
    app = create_realtime_app(
        settings=_settings(),
        provider_factory=lambda: provider,
    )
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 200
    with client.websocket_connect(
        "/v1/realtime-sessions:stream",
        headers={"authorization": "Bearer local"},
        subprotocols=["nomacook.ai-realtime.v1"],
    ) as websocket:
        websocket.send_text(json.dumps(_start()))
        ready = json.loads(websocket.receive_text())
        assert ready["message_type"] == "session.ready"


def test_readiness_fails_closed_without_provider_configuration() -> None:
    app = create_realtime_app(settings=_settings(provider_enabled=False))
    client = TestClient(app)
    assert client.get("/ready").status_code == 503


def test_production_entrypoint_registers_realtime_route() -> None:
    from server.gateway.main import app as production_app
    from server.gateway.main import create_production_app

    app = create_production_app(settings=_settings())
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert "/v1/realtime-sessions:stream" in paths
    production_paths = {route.path for route in production_app.routes if hasattr(route, "path")}
    assert "/v1/realtime-sessions:stream" in production_paths
    assert "/v1/agent-model:stream" in production_paths


def test_unready_production_entrypoint_rejects_before_provider_construction() -> None:
    calls = 0

    def provider_factory():
        nonlocal calls
        calls += 1
        raise AssertionError("provider must not be constructed while unready")

    from server.gateway.main import create_production_app

    app = create_production_app(
        settings=_settings(provider_enabled=False),
        provider_factory=provider_factory,
    )
    assert app.state.owners.active == 0
    assert calls == 0


def test_unready_websocket_is_rejected_before_provider_factory() -> None:
    calls = 0

    def provider_factory():
        nonlocal calls
        calls += 1
        raise AssertionError("provider must not be constructed")

    app = create_realtime_app(
        settings=_settings(provider_enabled=False),
        provider_factory=provider_factory,
    )
    client = TestClient(app)
    try:
        with client.websocket_connect(
            "/v1/realtime-sessions:stream",
            headers={"authorization": "Bearer local"},
            subprotocols=["nomacook.ai-realtime.v1"],
        ):
            raise AssertionError("unready websocket unexpectedly accepted")
    except Exception as exc:
        assert "4503" in str(exc) or getattr(exc, "code", None) == 4503
    assert calls == 0
