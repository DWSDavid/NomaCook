"""Deterministic ids, offline clock, and per-run artifact layout."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
import re

SESSION_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def session_id_for(video_path: str | Path, recipe_version_id: str) -> str:
    return f"ses_{_slug(recipe_version_id)}_{_slug(Path(video_path).stem)}"


def event_id_for(session_id: str, seq: int) -> str:
    return f"evt_{session_id}_{seq:08d}"


def t_server_for(pts_ms: float) -> datetime:
    return SESSION_EPOCH + timedelta(milliseconds=pts_ms)


@dataclass(frozen=True)
class SessionPaths:
    root: Path

    @property
    def events(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def keyframes_dir(self) -> Path:
        return self.root / "keyframes"

    @property
    def timeline(self) -> Path:
        return self.root / "timeline.jsonl"

    @property
    def annotated(self) -> Path:
        return self.root / "annotated.mp4"

    @property
    def report(self) -> Path:
        return self.root / "report.md"

    @property
    def meta(self) -> Path:
        return self.root / "meta.json"


def create_run_dir(
    session_id: str,
    base: Path = Path("data/sessions"),
    run_tag: str | None = None,
) -> SessionPaths:
    tag = run_tag or datetime.now().strftime("%Y%m%dT%H%M%S")
    root = base / session_id / f"run_{tag}"
    root.mkdir(parents=True, exist_ok=False)
    (root / "keyframes").mkdir()
    return SessionPaths(root=root)
