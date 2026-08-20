from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import AsyncIterator

from fastapi.testclient import TestClient
import httpx

from server.gateway.app import CapacityLimiter, ServiceSettings, create_app
from server.gateway.contracts import ModelRequest
from server.gateway.qwen_transport import ProviderChunk
from server.gateway.registry import ProviderCallRegistry
from server.gateway.service import AgentModelService


REPO = Path(__file__).resolve().parents[2]
REQUEST_BODY = (
    REPO / "server/gateway/contract/golden/request.json"
).read_bytes()


class FakeTransport:
    def __init__(self, chunks: list[ProviderChunk]) -> None:
        self.chunks = chunks
        self.calls = 0

    async def stream(self, request: ModelRequest) -> AsyncIterator[ProviderChunk]:
        self.calls += 1
        for chunk in self.chunks:
            yield chunk


def _settings(**overrides: object) -> ServiceSettings:
    values: dict[str, object] = {
        "service_token": "agent-service-secret",
        "max_concurrency": 2,
        "request_timeout_ms": 30000,
        "qwen_enabled": True,
        "qwen_api_key": "fake-key",
        "qwen_workspace_id": "fake-workspace",
        "qwen_model": "qwen3.6-flash",
        "host": "127.0.0.1",
        "port": 8099,
    }
    values.update(overrides)
    return ServiceSettings(**values)


def _client(*, settings: ServiceSettings | None = None, capacity: int = 2):
    transport = FakeTransport(
        [
            ProviderChunk(kind="text", text="hello"),
            ProviderChunk(kind="stop", finish_reason="stop"),
        ]
    )
    app = create_app(
        settings=settings or _settings(),
        service=AgentModelService(transport),
        registry=ProviderCallRegistry(recent_capacity=8),
        capacity=CapacityLimiter(capacity),
    )
    return TestClient(app), app, transport


def test_health_does_not_require_model_call() -> None:
    client, _, transport = _client()
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert transport.calls == 0


def test_ready_is_200_only_for_complete_qwen_configuration() -> None:
    ready_client, _, _ = _client()
    assert ready_client.get("/ready").status_code == 200

    missing_client, _, _ = _client(settings=_settings(qwen_api_key=""))
    assert missing_client.get("/ready").status_code == 503

    disabled_client, _, _ = _client(settings=_settings(qwen_enabled=False))
    assert disabled_client.get("/ready").status_code == 503

    wrong_profile, _, _ = _client(settings=_settings(qwen_model="qwen3.5-flash"))
    assert wrong_profile.get("/ready").status_code == 503


def test_missing_or_wrong_bearer_is_rejected_before_model_and_body() -> None:
    client, _, transport = _client()
    response = client.post(
        "/v1/agent-model:stream",
        content=b"not-json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 401
    assert transport.calls == 0

    wrong = client.post(
        "/v1/agent-model:stream",
        content=REQUEST_BODY,
        headers={
            "authorization": "Bearer wrong",
            "content-type": "application/json",
        },
    )
    assert wrong.status_code == 401
    assert transport.calls == 0


def test_body_limit_returns_413_before_model() -> None:
    client, _, transport = _client()
    body = b"{" + b"x" * (512 * 1024) + b"}"
    response = client.post(
        "/v1/agent-model:stream",
        content=body,
        headers={
            "authorization": "Bearer agent-service-secret",
            "content-type": "application/json",
        },
    )
    assert response.status_code == 413
    assert transport.calls == 0


def test_valid_request_is_ndjson_and_duplicate_is_409() -> None:
    client, _, transport = _client()
    headers = {
        "authorization": "Bearer agent-service-secret",
        "content-type": "application/json",
        "accept": "application/x-ndjson",
    }
    response = client.post("/v1/agent-model:stream", content=REQUEST_BODY, headers=headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in response.text.splitlines()]
    assert events[-1]["event_type"] == "message.end"
    assert transport.calls == 1

    duplicate = client.post("/v1/agent-model:stream", content=REQUEST_BODY, headers=headers)
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "DUPLICATE_PROVIDER_CALL"
    assert transport.calls == 1


def test_capacity_full_returns_service_busy() -> None:
    client, app, transport = _client(capacity=1)
    assert app.state.capacity.try_acquire() is True
    response = client.post(
        "/v1/agent-model:stream",
        content=REQUEST_BODY,
        headers={"authorization": "Bearer agent-service-secret"},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_BUSY"
    assert transport.calls == 0
    app.state.capacity.release()


def test_unknown_route_has_no_internal_detail() -> None:
    client, _, _ = _client()
    response = client.get("/missing")
    assert response.status_code == 404
    assert "Traceback" not in response.text


def test_environment_loader_uses_only_explicit_process_values() -> None:
    settings = ServiceSettings.from_environment(
        {
            "AI_MODEL_SERVICE_TOKEN": "token",
            "AI_MODEL_MAX_CONCURRENCY": "3",
            "AI_MODEL_REQUEST_TIMEOUT_MS": "1200",
            "QWEN_AGENT_ENABLED": "true",
            "DASHSCOPE_API_KEY": "key",
            "BAILIAN_WORKSPACE_ID": "workspace",
            "QWEN_AGENT_MODEL": "qwen3.6-flash",
            "AI_MODEL_SERVICE_HOST": "127.0.0.1",
            "AI_MODEL_SERVICE_PORT": "8123",
        }
    )
    assert settings.max_concurrency == 3
    assert settings.request_timeout_ms == 1200
    assert settings.port == 8123


def test_disconnect_path_releases_capacity_and_registry() -> None:
    # The route's async generator owns both resources; this direct ASGI stream
    # smoke ensures the normal close path leaves no active call behind.
    async def run() -> None:
        client, app, _ = _client(capacity=1)
        del client
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.post(
                "/v1/agent-model:stream",
                content=REQUEST_BODY,
                headers={"authorization": "Bearer agent-service-secret"},
            )
            assert response.status_code == 200
        assert app.state.capacity.active == 0

    asyncio.run(run())
