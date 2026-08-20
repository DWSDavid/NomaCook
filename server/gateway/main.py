"""Uvicorn entrypoint for the Python AI Model Service."""

from __future__ import annotations

import uvicorn

from .app import ServiceSettings, create_app


def main() -> None:
    settings = ServiceSettings.from_environment()
    uvicorn.run(
        create_app(settings=settings),
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
