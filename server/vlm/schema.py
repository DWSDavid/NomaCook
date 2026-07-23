"""Request, response, and stale-result contracts for VLM confirmation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from server.events import EventEnvelope, create_event


DEFAULT_VLM_TTL_SECONDS = 8


class VLMDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    step_id: str = Field(min_length=1)
    context_version: int = Field(ge=1)
    frame_id: str = Field(min_length=1)
    requested_at: datetime
    expires_at: datetime
    completion_check: str = Field(min_length=1)
    expected_objects: tuple[str, ...] = ()

    @field_validator("requested_at", "expires_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_expiry(self) -> "VLMDecisionRequest":
        if self.expires_at <= self.requested_at:
            raise ValueError("expires_at must be after requested_at")
        return self

    @classmethod
    def create(
        cls,
        *,
        decision_id: str,
        session_id: str,
        step_id: str,
        context_version: int,
        frame_id: str,
        requested_at: datetime,
        completion_check: str,
        expected_objects: tuple[str, ...] = (),
        ttl_seconds: int = DEFAULT_VLM_TTL_SECONDS,
    ) -> "VLMDecisionRequest":
        return cls(
            decision_id=decision_id,
            session_id=session_id,
            step_id=step_id,
            context_version=context_version,
            frame_id=frame_id,
            requested_at=requested_at,
            expires_at=requested_at + timedelta(seconds=ttl_seconds),
            completion_check=completion_check,
            expected_objects=expected_objects,
        )


class VLMObservation(BaseModel):
    """The only model-generated fields accepted by the state boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: str
    step_id: str
    context_version: int = Field(ge=1)
    frame_id: str
    phase: Literal["not_started", "in_progress", "likely_complete"]
    confidence: float = Field(ge=0.0, le=1.0)
    observed_objects: tuple[str, ...] = ()
    risk_level: Literal["none", "warning", "critical"] = "none"
    risk_reason: str | None = None
    reason: str = Field(min_length=1, max_length=300)


class ValidatedVLMResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["accepted", "stale"]
    stale_reason: Literal[
        "ttl_expired",
        "decision_mismatch",
        "step_mismatch",
        "context_version_mismatch",
        "frame_mismatch",
    ] | None = None
    request: VLMDecisionRequest
    observation: VLMObservation
    received_at: datetime

    def to_event(
        self,
        *,
        seq: int,
        t_device_ms: float,
        source: str,
    ) -> EventEnvelope:
        payload = {
            **self.observation.model_dump(mode="json"),
            "validation_status": self.status,
            "stale_reason": self.stale_reason,
        }
        return create_event(
            session_id=self.request.session_id,
            seq=seq,
            event_type=(
                "vlm.step_assessment"
                if self.status == "accepted"
                else "vlm.step_assessment.stale"
            ),
            t_device_ms=t_device_ms,
            t_server_est=self.request.requested_at,
            received_at=self.received_at,
            frame_id=self.request.frame_id,
            source=source,
            backfill=self.stale_reason == "ttl_expired",
            confidence=self.observation.confidence,
            payload=payload,
        )


def validate_observation(
    request: VLMDecisionRequest,
    observation: VLMObservation,
    *,
    received_at: datetime,
) -> ValidatedVLMResult:
    if received_at.tzinfo is None or received_at.utcoffset() is None:
        raise ValueError("received_at must include a timezone")
    received_at = received_at.astimezone(UTC)

    stale_reason = None
    if received_at > request.expires_at:
        stale_reason = "ttl_expired"
    elif observation.decision_id != request.decision_id:
        stale_reason = "decision_mismatch"
    elif observation.step_id != request.step_id:
        stale_reason = "step_mismatch"
    elif observation.context_version != request.context_version:
        stale_reason = "context_version_mismatch"
    elif observation.frame_id != request.frame_id:
        stale_reason = "frame_mismatch"

    return ValidatedVLMResult(
        status="stale" if stale_reason else "accepted",
        stale_reason=stale_reason,
        request=request,
        observation=observation,
        received_at=received_at,
    )
