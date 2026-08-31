"""Versioned JSON contracts for the single NomaChef session timeline."""

from __future__ import annotations

from datetime import UTC, datetime
import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from ulid import ULID


EVENT_SCHEMA_VERSION = "1.0"


def _prefixed_ulid(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


class AudioRange(BaseModel):
    """A half-open sample range in one session audio stream."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start_sample: int = Field(ge=0)
    end_sample: int = Field(gt=0)
    sample_rate_hz: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_range(self) -> "AudioRange":
        if self.end_sample <= self.start_sample:
            raise ValueError("end_sample must be greater than start_sample")
        return self


class EvidencePayload(BaseModel):
    """Structured perception evidence; raw signals stay separate from conclusions."""

    model_config = ConfigDict(extra="allow", frozen=True)

    relation: str = Field(min_length=1)
    phase: str | None = None
    hand: Literal["left", "right", "unknown"] | None = None
    object_class: str | None = None
    relation_confidence: float = Field(ge=0.0, le=1.0)
    phase_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    signals: dict[str, float] = Field(default_factory=dict)

    @field_validator("signals")
    @classmethod
    def validate_signals(cls, signals: dict[str, float]) -> dict[str, float]:
        for name, value in signals.items():
            if not name:
                raise ValueError("signal names cannot be empty")
            if not math.isfinite(value):
                raise ValueError(f"signal {name!r} must be finite")
        return signals


class EventEnvelope(BaseModel):
    """One immutable fact on a session's ordered, replayable event stream."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    seq: int = Field(ge=0)
    type: str = Field(min_length=1)
    t_device_ms: float = Field(ge=0.0)
    t_server_est: datetime
    received_at: datetime
    frame_id: str | None = None
    audio_range: AudioRange | None = None
    source: str = Field(min_length=1)
    schema_version: Literal[EVENT_SCHEMA_VERSION] = EVENT_SCHEMA_VERSION
    backfill: bool = False
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    payload: dict[str, Any] = Field(default_factory=dict)
    context_version: int | None = Field(default=None, ge=1)
    runtime_mode: Literal["RUN", "SHADOW", "REPLAY_EVAL"] = "RUN"

    @field_validator("t_device_ms")
    @classmethod
    def validate_device_time(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("t_device_ms must be finite")
        return value

    @field_validator("t_server_est", "received_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must include a timezone")
        return value.astimezone(UTC)

    def canonical_dict(self, *, include_received_at: bool = False) -> dict[str, Any]:
        """Return JSON-compatible content used for deterministic replay diffs."""

        excluded = set() if include_received_at else {"received_at"}
        return self.model_dump(mode="json", exclude=excluded)


def create_event(
    *,
    session_id: str,
    seq: int,
    event_type: str,
    t_device_ms: float,
    t_server_est: datetime,
    source: str,
    payload: BaseModel | dict[str, Any],
    event_id: str | None = None,
    received_at: datetime | None = None,
    frame_id: str | None = None,
    audio_range: AudioRange | None = None,
    backfill: bool = False,
    confidence: float | None = None,
    context_version: int | None = None,
    runtime_mode: Literal["RUN", "SHADOW", "REPLAY_EVAL"] = "RUN",
) -> EventEnvelope:
    """Create a validated envelope at the session-service boundary."""

    if isinstance(payload, BaseModel):
        payload_dict = payload.model_dump(mode="json")
    else:
        payload_dict = payload

    return EventEnvelope(
        event_id=event_id or _prefixed_ulid("evt"),
        session_id=session_id,
        seq=seq,
        type=event_type,
        t_device_ms=t_device_ms,
        t_server_est=t_server_est,
        received_at=received_at or datetime.now(UTC),
        frame_id=frame_id,
        audio_range=audio_range,
        source=source,
        backfill=backfill,
        confidence=confidence,
        payload=payload_dict,
        context_version=context_version,
        runtime_mode=runtime_mode,
    )
