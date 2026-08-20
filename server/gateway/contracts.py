"""Strict request and event DTOs for AI Model Service v1."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import Any, ClassVar, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)
from typing_extensions import Annotated


CONTRACT_VERSION = "ai-model-service.contract.v1"
SCHEMA_VERSION = "1.0"
CAPABILITY = "agent_tool_stream_v1"
MAX_REQUEST_BYTES = 512 * 1024
MAX_MESSAGE_TEXT_BYTES = 256 * 1024
MAX_TOOL_SCHEMA_BYTES = 32 * 1024
MAX_TEXT_DELTA_BYTES = 8 * 1024

APPROVED_TOOLS = frozenset({"nomacook.speak@1", "nomacook.submit_decision@1"})
EVENT_TYPES = frozenset(
    {
        "response.accepted",
        "message.start",
        "text.delta",
        "tool.call",
        "usage",
        "response.heartbeat",
        "message.end",
        "response.failed",
        "response.cancelled",
    }
)
TERMINAL_EVENT_TYPES = frozenset(
    {"message.end", "response.failed", "response.cancelled"}
)
STOP_REASONS = frozenset({"stop", "tool_call", "length", "content_filter"})

RequestId = Annotated[str, StringConstraints(min_length=1, max_length=128)]


def _json_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _parse_json_strict(value: str | bytes | bytearray) -> Any:
    if isinstance(value, bytearray):
        value = bytes(value)
    return json.loads(value, object_pairs_hook=_json_no_duplicates)


def _json_size(value: Any) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
    )


def _walk_schema(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"$ref", "$dynamicRef"}:
                raise ValueError("external or recursive schema references are forbidden")
            if key == "$schema" and child != "https://json-schema.org/draft/2020-12/schema":
                raise ValueError("unsupported JSON Schema draft")
            _walk_schema(child)
    elif isinstance(value, list):
        for child in value:
            _walk_schema(child)


class ContractDTO(BaseModel):
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
        """Parse JSON with duplicate-key rejection before Pydantic validation."""

        parsed = _parse_json_strict(json_data)
        return cls.model_validate(
            parsed,
            # JSON decoding already enforces the wire types. Pydantic's JSON
            # mode additionally parses RFC3339 timestamps and JSON arrays;
            # preserve that behavior while keeping direct Python validation
            # strict by default.
            strict=False if strict is None else strict,
            context=context,
            by_alias=by_alias,
            by_name=by_name,
        )


class ModelMessage(ContractDTO):
    role: Literal["system", "user"]
    content: str

    @field_validator("content")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_MESSAGE_TEXT_BYTES:
            raise ValueError("message content exceeds 256 KiB")
        return value


class ModelTool(ContractDTO):
    name: Literal["nomacook.speak@1", "nomacook.submit_decision@1"]
    description: str
    parameters: dict[str, Any]

    @field_validator("parameters")
    @classmethod
    def validate_parameters(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict) or value.get("type") != "object":
            raise ValueError("Tool parameters must be an object JSON Schema")
        if _json_size(value) > MAX_TOOL_SCHEMA_BYTES:
            raise ValueError("Tool schema exceeds 32 KiB")
        _walk_schema(value)
        return value


class ModelOptions(ContractDTO):
    max_output_tokens: int = Field(ge=1, le=4096)
    temperature: float = Field(ge=0.0, le=1.0)
    tool_choice: Literal["auto", "required", "none"]


class ModelRequest(ContractDTO):
    contract_version: Literal[CONTRACT_VERSION]
    schema_version: Literal[SCHEMA_VERSION]
    request_id: RequestId
    turn_id: RequestId
    provider_call_id: RequestId
    capability: Literal[CAPABILITY]
    started_at: datetime
    deadline_at: datetime
    timeout_ms: int = Field(ge=1, le=60000)
    messages: tuple[ModelMessage, ...] = Field(max_length=8)
    tools: tuple[ModelTool, ...] = Field(max_length=2)
    options: ModelOptions

    _timestamp_fields: ClassVar[tuple[str, ...]] = ("started_at", "deadline_at")

    @field_validator("started_at", "deadline_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        if self.deadline_at <= self.started_at:
            raise ValueError("deadline_at must be later than started_at")
        total_text = sum(len(message.content.encode("utf-8")) for message in self.messages)
        if total_text > MAX_MESSAGE_TEXT_BYTES:
            raise ValueError("message text exceeds 256 KiB")
        return self


class ModelError(ContractDTO):
    code: Literal[
        "INVALID_REQUEST",
        "CONTRACT_VERSION_UNSUPPORTED",
        "PAYLOAD_TOO_LARGE",
        "INVALID_SERVICE_TOKEN",
        "DUPLICATE_PROVIDER_CALL",
        "MODEL_UNAVAILABLE",
        "MODEL_RATE_LIMITED",
        "MODEL_TIMEOUT",
        "MODEL_RESPONSE_INVALID",
        "CONTENT_FILTERED",
        "REQUEST_CANCELLED",
        "SERVICE_BUSY",
        "AI_MODEL_SERVICE_ERROR",
    ]
    retryable: bool
    phase: str
    message: str
    retry_after_ms: int | None = Field(default=None, ge=0, le=60000)


class ModelEvent(ContractDTO):
    contract_version: Literal[CONTRACT_VERSION]
    schema_version: Literal[SCHEMA_VERSION]
    request_id: RequestId
    turn_id: RequestId
    provider_call_id: RequestId
    stream_sequence: int = Field(ge=1)
    event_type: Literal[
        "response.accepted",
        "message.start",
        "text.delta",
        "tool.call",
        "usage",
        "response.heartbeat",
        "message.end",
        "response.failed",
        "response.cancelled",
    ]
    occurred_at: datetime
    data: dict[str, Any] = Field(default_factory=dict)
    error: ModelError | None = None

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_event_data(self) -> Self:
        if self.event_type == "text.delta":
            text = self.data.get("text")
            if not isinstance(text, str):
                raise ValueError("text.delta requires string text")
            if len(text.encode("utf-8")) > MAX_TEXT_DELTA_BYTES:
                raise ValueError("text.delta exceeds 8 KiB")
        if self.event_type == "message.end":
            stop_reason = self.data.get("stop_reason")
            if stop_reason not in STOP_REASONS:
                raise ValueError("invalid message.end stop_reason")
        if self.event_type in {"response.failed", "response.cancelled"} and self.error is None:
            raise ValueError("terminal failure/cancellation requires error")
        if self.event_type not in {"response.failed", "response.cancelled"} and self.error is not None:
            raise ValueError("error is only valid on failed/cancelled responses")
        if _contains_thinking(self.data):
            raise ValueError("thinking/reasoning content is forbidden")
        return self


def _contains_thinking(value: Any) -> bool:
    if isinstance(value, dict):
        if any(key in {"thinking", "reasoning", "reasoning_content"} for key in value):
            return True
        return any(_contains_thinking(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_thinking(child) for child in value)
    return False
