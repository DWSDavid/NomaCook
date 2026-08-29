"""Uvicorn entrypoint for the Python AI Model Service."""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI

from .app import ServiceSettings, create_app as create_agent_app
from .realtime_app import RealtimeSettings, create_realtime_app


def create_production_app(*, settings: RealtimeSettings | None = None,
                          provider_factory=None, codec_factory=None):
    """Build one Uvicorn app while retaining the existing Agent Model route."""

    resolved = settings or RealtimeSettings.from_environment()
    kwargs = {"settings": resolved}
    if provider_factory is not None:
        kwargs["provider_factory"] = provider_factory
    if codec_factory is not None:
        kwargs["codec_factory"] = codec_factory
    realtime = create_realtime_app(**kwargs)
    agent = create_agent_app(settings=ServiceSettings.from_environment())
    combined = FastAPI(title="NomaCook AI Services", version="1.0")
    # The Realtime app owns the shared health/readiness meaning. Keep the
    # previously shipped Agent Model endpoint without duplicating those paths.
    for route in agent.routes:
        if getattr(route, "path", None) in {"/health", "/ready"}:
            continue
        combined.router.routes.append(route)
    combined.router.routes.extend(realtime.routes)
    combined.state.realtime = realtime.state
    combined.state.agent = agent.state
    combined.state.owners = realtime.state.owners
    combined.state.codec_ready = realtime.state.codec_ready
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
