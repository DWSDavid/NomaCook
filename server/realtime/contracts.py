"""Strict control envelopes and binary media framing for Realtime v1."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import struct
from typing import Any, ClassVar, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


CONTRACT_VERSION = "ai-realtime.contract.v1"
SCHEMA_VERSION = "1.1"
SUBPROTOCOL = "nomacook.ai-realtime.v1"
MAX_CONTROL_FRAME_BYTES = 64 * 1024
MAX_CONTEXT_BYTES = 32 * 1024
MAX_BINARY_FRAME_BYTES = 64 * 1024
MIN_BINARY_FRAME_BYTES = 13
RTP_FRAME_SAMPLES = 960

CONTROL_MESSAGE_TYPES = frozenset(
    {
        "session.start",
        "context.update",
        "announce",
        "session.pause",
        "session.resume",
        "session.stop",
        "session.ready",
        "input.speech_started",
        "input.speech_stopped",
        "response.thinking",
        "response.assistant_text",
        "response.audio_started",
        "response.audio_done",
        "response.interrupted",
        "announce.completed",
        "announce.failed",
        "session.failed",
        "session.closed",
    }
)
STOP_REASONS = frozenset(
    {"force_stop", "terminal", "peer_lost", "shutdown", "generation_replaced"}
)


def _json_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _parse_json(value: str | bytes | bytearray) -> Any:
    if isinstance(value, bytearray):
        value = bytes(value)
    if len(value) > MAX_CONTROL_FRAME_BYTES:
        raise ValueError("control frame exceeds 64 KiB")
    return json.loads(value, object_pairs_hook=_json_no_duplicates)


def _json_size(value: Any) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )


class StrictDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    @classmethod
    def model_validate_json(
        cls,
        json_data: str | bytes | bytearray,
        *,
        strict: bool | None = None,
        context: dict[str, Any] | None = None,
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Self:
        parsed = _parse_json(json_data)
        return cls.model_validate(
            parsed,
            strict=False if strict is None else strict,
            context=context,
            by_alias=by_alias,
            by_name=by_name,
        )


class AudioProfile(StrictDTO):
    encoding: Literal["opus"]
    clock_rate_hz: Literal[48000]
    channels: Literal[2]
    frame_duration_ms: Literal[20]


class SessionLimits(StrictDTO):
    max_binary_frame_bytes: int = Field(ge=MIN_BINARY_FRAME_BYTES, le=MAX_BINARY_FRAME_BYTES)
    max_buffered_audio_ms: int = Field(ge=20, le=1000)


class SessionStartPayload(StrictDTO):
    started_at: datetime
    deadline_at: datetime
    input_audio: AudioProfile
    output_audio: AudioProfile
    context_revision: int = Field(ge=1)
    context: dict[str, Any]
    limits: SessionLimits

    @field_validator("started_at", "deadline_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_deadline_and_context(self) -> Self:
        if self.deadline_at <= self.started_at:
            raise ValueError("deadline_at must be later than started_at")
        if _json_size(self.context) > MAX_CONTEXT_BYTES:
            raise ValueError("context exceeds 32 KiB")
        return self


class ContextUpdatePayload(StrictDTO):
    context_revision: int = Field(ge=1)
    context: dict[str, Any]

    @model_validator(mode="after")
    def validate_context_size(self) -> Self:
        if _json_size(self.context) > MAX_CONTEXT_BYTES:
            raise ValueError("context exceeds 32 KiB")
        return self


class AnnouncePayload(StrictDTO):
    utterance_id: str = Field(min_length=1, max_length=128)
    message_ref: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=1000)
    deadline_at: datetime

    @field_validator("deadline_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("deadline_at must include a timezone")
        return value.astimezone(UTC)


class SessionStopPayload(StrictDTO):
    reason: str

    @field_validator("reason")
    @classmethod
    def require_stop_reason(cls, value: str) -> str:
        if value not in STOP_REASONS:
            raise ValueError("invalid stop reason")
        return value


class ResponseAssistantTextPayload(StrictDTO):
    utterance_id: str = Field(min_length=1, max_length=128)
    text: str = Field(max_length=1000)
    message_ref: str | None = Field(default=None, min_length=1, max_length=128)


class ResponseAudioStartedPayload(StrictDTO):
    utterance_id: str = Field(min_length=1, max_length=128)
    message_ref: str | None = Field(default=None, min_length=1, max_length=128)


class ResponseAudioDonePayload(StrictDTO):
    utterance_id: str = Field(min_length=1, max_length=128)
    output_frame_count: int = Field(gt=0)


class AnnounceResultPayload(StrictDTO):
    utterance_id: str = Field(min_length=1, max_length=128)
    message_ref: str = Field(min_length=1, max_length=128)
    code: str | None = Field(default=None, min_length=1, max_length=128)


class RealtimeEnvelope(StrictDTO):
    contract_version: Literal[CONTRACT_VERSION]
    schema_version: Literal[SCHEMA_VERSION]
    session_id: UUID
    session_generation: int = Field(gt=0)
    producer: Literal["backend", "ai_service"]
    stream_sequence: int = Field(ge=1)
    message_type: Literal[
        "session.start",
        "context.update",
        "announce",
        "session.pause",
        "session.resume",
        "session.stop",
        "session.ready",
        "input.speech_started",
        "input.speech_stopped",
        "response.thinking",
        "response.assistant_text",
        "response.audio_started",
        "response.audio_done",
        "response.interrupted",
        "announce.completed",
        "announce.failed",
        "session.failed",
        "session.closed",
    ]
    occurred_at: datetime
    payload: dict[str, Any]

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_message_payload(self) -> Self:
        if self.message_type == "session.start":
            SessionStartPayload.model_validate(self.payload, strict=False)
        elif self.message_type == "context.update":
            ContextUpdatePayload.model_validate(self.payload, strict=False)
        elif self.message_type == "announce":
            AnnouncePayload.model_validate(self.payload, strict=False)
        elif self.message_type == "session.stop":
            SessionStopPayload.model_validate(self.payload, strict=False)
        elif self.message_type == "response.assistant_text":
            ResponseAssistantTextPayload.model_validate(self.payload, strict=False)
        elif self.message_type == "response.audio_started":
            ResponseAudioStartedPayload.model_validate(self.payload, strict=False)
        elif self.message_type == "response.audio_done":
            ResponseAudioDonePayload.model_validate(self.payload, strict=False)
        elif self.message_type in {"announce.completed", "announce.failed"}:
            AnnounceResultPayload.model_validate(self.payload, strict=False)
        elif self.message_type in {"session.pause", "session.resume"} and self.payload:
            raise ValueError("lifecycle pause/resume payload must be empty")
        return self


def parse_control_json(value: str | bytes | bytearray) -> RealtimeEnvelope:
    parsed = _parse_json(value)
    return RealtimeEnvelope.model_validate(parsed, strict=False)


@dataclass(frozen=True)
class BinaryAudioFrame:
    kind: Literal["input_opus", "output_opus"]
    packet_sequence: int
    rtp_timestamp: int
    payload: bytes

    VERSION: ClassVar[int] = 1
    HEADER: ClassVar[struct.Struct] = struct.Struct(">BBHII")

    def __post_init__(self) -> None:
        if self.kind not in {"input_opus", "output_opus"}:
            raise ValueError("invalid binary frame kind")
        if not 0 <= self.packet_sequence <= 0xFFFFFFFF:
            raise ValueError("packet_sequence out of range")
        if not 0 <= self.rtp_timestamp <= 0xFFFFFFFF:
            raise ValueError("rtp_timestamp out of range")
        if not self.payload:
            raise ValueError("audio payload cannot be empty")
        if self.HEADER.size + len(self.payload) > MAX_BINARY_FRAME_BYTES:
            raise ValueError("binary frame exceeds 64 KiB")

    def to_bytes(self) -> bytes:
        kind = 1 if self.kind == "input_opus" else 2
        return self.HEADER.pack(
            self.VERSION, kind, 0, self.packet_sequence, self.rtp_timestamp
        ) + self.payload

    @classmethod
    def from_bytes(cls, raw: bytes) -> "BinaryAudioFrame":
        if not MIN_BINARY_FRAME_BYTES <= len(raw) <= MAX_BINARY_FRAME_BYTES:
            raise ValueError("binary frame size out of range")
        version, kind, flags, sequence, timestamp = cls.HEADER.unpack(raw[: cls.HEADER.size])
        if version != cls.VERSION or flags != 0:
            raise ValueError("invalid binary frame header")
        if kind not in {1, 2}:
            raise ValueError("invalid binary frame kind")
        return cls(
            kind="input_opus" if kind == 1 else "output_opus",
            packet_sequence=sequence,
            rtp_timestamp=timestamp,
            payload=raw[cls.HEADER.size :],
        )
