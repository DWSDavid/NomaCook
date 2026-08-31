"""Uvicorn entrypoint for the Python AI Model Service."""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .app import ServiceSettings, create_app as create_agent_app
from .realtime_app import RealtimeSettings, create_realtime_app


def create_production_app(*, settings: RealtimeSettings | None = None,
                          provider_factory=None, codec_factory=None,
                          agent_settings: ServiceSettings | None = None,
                          agent_service=None):
    """Build one Uvicorn app while retaining the existing Agent Model route."""

    resolved = settings or RealtimeSettings.from_environment()
    resolved_agent = agent_settings or ServiceSettings.from_environment()
    kwargs = {"settings": resolved}
    if provider_factory is not None:
        kwargs["provider_factory"] = provider_factory
    if codec_factory is not None:
        kwargs["codec_factory"] = codec_factory
    realtime = create_realtime_app(**kwargs)
    agent = create_agent_app(settings=resolved_agent, service=agent_service)
    combined = FastAPI(title="NomaCook AI Services", version="1.0")
    # Keep both previously shipped model endpoints without duplicating the
    # component-local health/readiness routes.
    for route in agent.routes:
        if getattr(route, "path", None) in {"/health", "/ready"}:
            continue
        combined.router.routes.append(route)
    for route in realtime.routes:
        if getattr(route, "path", None) in {"/health", "/ready"}:
            continue
        combined.router.routes.append(route)
    combined.state.realtime = realtime.state
    combined.state.agent = agent.state
    combined.state.owners = realtime.state.owners
    combined.state.codec_ready = realtime.state.codec_ready

    @combined.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @combined.get("/ready")
    async def ready() -> JSONResponse:
        agent_ready = (
            bool(getattr(combined.state.agent, "settings", None).ready)
            if getattr(combined.state.agent, "settings", None) is not None
            else False
        ) and getattr(combined.state.agent, "service", None) is not None
        realtime_state = combined.state.realtime
        realtime_ready = (
            bool(realtime_state.settings.ready)
            and bool(realtime_state.codec_ready)
        )
        if not (agent_ready and realtime_ready):
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "MODEL_UNAVAILABLE",
                        "retryable": True,
                        "phase": "readiness",
                        "message": "ai services unavailable",
                    }
                },
            )
        return JSONResponse(status_code=200, content={"status": "ready"})

    return combined


app = create_production_app()


def main() -> None:
    settings = RealtimeSettings.from_environment()
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
