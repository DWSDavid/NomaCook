"""FastAPI surface for the standalone AI Model Service."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
import threading
from typing import AsyncIterator, Mapping

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .auth import verify_service_bearer
from .contracts import MAX_REQUEST_BYTES, ModelRequest
from .errors import ModelServiceError
from .qwen_transport import QwenAgentConfig, QwenAgentTransport
from .registry import ProviderCallRegistry
from .service import AgentModelService


def _placeholder(value: str) -> bool:
    return value.strip().lower() in {
        "",
        "placeholder",
        "changeme",
        "your_api_key",
        "your_workspace_id",
        "none",
        "null",
    }


@dataclass(frozen=True)
class ServiceSettings:
    service_token: str
    max_concurrency: int
    request_timeout_ms: int
    qwen_enabled: bool
    qwen_api_key: str
    qwen_workspace_id: str
    qwen_model: str
    host: str
    port: int

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> "ServiceSettings":
        values = dict(os.environ if environment is None else environment)
        return cls(
            service_token=values.get("AI_MODEL_SERVICE_TOKEN", ""),
            max_concurrency=_int_env(values, "AI_MODEL_MAX_CONCURRENCY", 4, minimum=1, maximum=256),
            request_timeout_ms=_int_env(values, "AI_MODEL_REQUEST_TIMEOUT_MS", 60000, minimum=1, maximum=60000),
            qwen_enabled=values.get("QWEN_AGENT_ENABLED", "false").lower() == "true",
            qwen_api_key=values.get("DASHSCOPE_API_KEY", ""),
            qwen_workspace_id=values.get("BAILIAN_WORKSPACE_ID", ""),
            qwen_model=values.get("QWEN_AGENT_MODEL", ""),
            host=values.get("AI_MODEL_SERVICE_HOST", "127.0.0.1"),
            port=_int_env(values, "AI_MODEL_SERVICE_PORT", 8090, minimum=1, maximum=65535),
        )

    @property
    def ready(self) -> bool:
        return (
            bool(self.service_token)
            and self.qwen_enabled
            and not _placeholder(self.qwen_api_key)
            and not _placeholder(self.qwen_workspace_id)
            and self.qwen_model == "qwen3.6-flash"
        )


def _int_env(
    values: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = values.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, value))


class CapacityLimiter:
    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._active = 0
        self._lock = threading.Lock()

    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    def try_acquire(self) -> bool:
        with self._lock:
            if self._active >= self.capacity:
                return False
            self._active += 1
            return True

    def release(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)


def create_app(
    *,
    settings: ServiceSettings,
    service: AgentModelService | None = None,
    registry: ProviderCallRegistry | None = None,
    capacity: CapacityLimiter | None = None,
) -> FastAPI:
    if service is None and settings.ready:
        transport = QwenAgentTransport(
            QwenAgentConfig(
                api_key=settings.qwen_api_key,
                workspace_id=settings.qwen_workspace_id,
                model=settings.qwen_model,
                timeout_seconds=settings.request_timeout_ms / 1000.0,
            )
        )
        service = AgentModelService(transport)

    app = FastAPI(title="NomaCook AI Model Service", version="1.0")
    app.state.settings = settings
    app.state.service = service
    app.state.registry = registry or ProviderCallRegistry()
    app.state.capacity = capacity or CapacityLimiter(settings.max_concurrency)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> JSONResponse:
        if not settings.ready or app.state.service is None:
            return JSONResponse(
                status_code=503,
                content={"error": _error("MODEL_UNAVAILABLE", "readiness unavailable")},
            )
        return JSONResponse(status_code=200, content={"status": "ready"})

    @app.post("/v1/agent-model:stream")
    async def model_stream(request: Request):
        if not verify_service_bearer(
            request.headers.get("authorization"), settings.service_token
        ):
            return JSONResponse(
                status_code=401,
                content={"error": _error("INVALID_SERVICE_TOKEN", "invalid service token")},
            )

        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > MAX_REQUEST_BYTES:
                    return JSONResponse(
                        status_code=413,
                        content={"error": _error("PAYLOAD_TOO_LARGE", "request body too large")},
                    )
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={"error": _error("INVALID_REQUEST", "invalid content length")},
                )

        body = await request.body()
        if len(body) > MAX_REQUEST_BYTES:
            return JSONResponse(
                status_code=413,
                content={"error": _error("PAYLOAD_TOO_LARGE", "request body too large")},
            )
        try:
            model_request = ModelRequest.model_validate_json(body)
        except Exception:
            return JSONResponse(
                status_code=400,
                content={"error": _error("INVALID_REQUEST", "request does not match contract")},
            )
        if app.state.service is None or not settings.ready:
            return JSONResponse(
                status_code=503,
                content={"error": _error("MODEL_UNAVAILABLE", "model service unavailable")},
            )
        if not app.state.capacity.try_acquire():
            return JSONResponse(
                status_code=503,
                content={"error": _error("SERVICE_BUSY", "service capacity is full")},
            )
        if not await app.state.registry.admit(model_request.provider_call_id):
            app.state.capacity.release()
            return JSONResponse(
                status_code=409,
                content={"error": _error("DUPLICATE_PROVIDER_CALL", "provider call already used")},
            )

        async def generate() -> AsyncIterator[bytes]:
            try:
                async for line in app.state.service.stream(model_request):
                    yield line
            except asyncio.CancelledError:
                raise
            finally:
                app.state.capacity.release()
                await app.state.registry.complete(model_request.provider_call_id)

        return StreamingResponse(generate(), media_type="application/x-ndjson")

    return app


def _error(code: str, message: str) -> dict[str, object]:
    return ModelServiceError(
        code=code,  # type: ignore[arg-type]
        retryable=code in {"SERVICE_BUSY", "MODEL_UNAVAILABLE"},
        phase="request",
        message=message,
    ).to_model_error().model_dump(mode="json")
