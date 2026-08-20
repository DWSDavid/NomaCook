from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from server.gateway.contracts import ModelEvent, ModelRequest


REPO = Path(__file__).resolve().parents[2]
GOLDEN = REPO / "server" / "gateway" / "contract" / "golden"


def _request_dict() -> dict:
    return {
        "contract_version": "ai-model-service.contract.v1",
        "schema_version": "1.0",
        "request_id": "request_demo_01",
        "turn_id": "turn_demo_01",
        "provider_call_id": "provider_call_demo_01",
        "capability": "agent_tool_stream_v1",
        "started_at": "2026-08-20T10:00:00Z",
        "deadline_at": "2026-08-20T10:00:30Z",
        "timeout_ms": 30000,
        "messages": [
            {"role": "system", "content": "You are a cooking assistant."},
            {"role": "user", "content": "What should I do next?"},
        ],
        "tools": [
            {
                "name": "nomacook.speak@1",
                "description": "Speak a short grounded instruction.",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
            }
        ],
        "options": {
            "max_output_tokens": 1024,
            "temperature": 0.2,
            "tool_choice": "auto",
        },
    }


def test_valid_request_golden_is_accepted() -> None:
    raw = (GOLDEN / "request.json").read_text(encoding="utf-8")
    request = ModelRequest.model_validate_json(raw)
    assert request.capability == "agent_tool_stream_v1"
    assert request.schema_version == "1.0"


def test_valid_request_dict_is_accepted() -> None:
    request = ModelRequest.model_validate_json(json.dumps(_request_dict()))
    assert request.request_id == "request_demo_01"
    assert len(request.messages) == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("unexpected", True),
        ("capability", "other_capability"),
        ("messages", [{"role": "assistant", "content": "no"}]),
    ],
)
def test_unknown_field_role_or_capability_is_rejected(field: str, value: object) -> None:
    payload = _request_dict()
    payload[field] = value
    with pytest.raises(ValidationError):
        ModelRequest.model_validate_json(json.dumps(payload))


def test_request_message_tool_and_timeout_bounds_are_rejected() -> None:
    too_many_messages = _request_dict()
    too_many_messages["messages"] = [
        {"role": "user", "content": str(i)} for i in range(9)
    ]
    with pytest.raises(ValidationError):
        ModelRequest.model_validate_json(json.dumps(too_many_messages))

    too_many_tools = _request_dict()
    too_many_tools["tools"] = [
        _request_dict()["tools"][0],
        _request_dict()["tools"][0],
        _request_dict()["tools"][0],
    ]
    with pytest.raises(ValidationError):
        ModelRequest.model_validate_json(json.dumps(too_many_tools))

    bad_timeout = _request_dict()
    bad_timeout["timeout_ms"] = 60001
    with pytest.raises(ValidationError):
        ModelRequest.model_validate_json(json.dumps(bad_timeout))


def test_media_provider_fields_and_external_schema_ref_are_rejected() -> None:
    media = _request_dict()
    media["messages"] = [
        {"role": "user", "content": [{"type": "image", "url": "x"}]}
    ]
    with pytest.raises(ValidationError):
        ModelRequest.model_validate_json(json.dumps(media))

    provider_fields = _request_dict()
    provider_fields["model"] = "qwen3.6-flash"
    with pytest.raises(ValidationError):
        ModelRequest.model_validate_json(json.dumps(provider_fields))

    external_ref = _request_dict()
    external_ref["tools"][0]["parameters"] = {
        "$ref": "https://example.invalid/schema.json",
        "type": "object",
    }
    with pytest.raises(ValidationError):
        ModelRequest.model_validate_json(json.dumps(external_ref))


def test_message_text_total_limit_is_rejected() -> None:
    payload = _request_dict()
    payload["messages"] = [{"role": "user", "content": "x" * (256 * 1024 + 1)}]
    with pytest.raises(ValidationError):
        ModelRequest.model_validate_json(json.dumps(payload))


def test_naive_timestamp_and_deadline_before_start_are_rejected() -> None:
    naive = _request_dict()
    naive["started_at"] = "2026-08-20T10:00:00"
    with pytest.raises(ValidationError):
        ModelRequest.model_validate_json(json.dumps(naive))

    reversed_deadline = _request_dict()
    reversed_deadline["deadline_at"] = "2026-08-20T09:59:59Z"
    with pytest.raises(ValidationError):
        ModelRequest.model_validate_json(json.dumps(reversed_deadline))


def test_invalid_event_sequence_stop_reason_and_thinking_are_rejected() -> None:
    base = {
        "contract_version": "ai-model-service.contract.v1",
        "schema_version": "1.0",
        "request_id": "request_demo_01",
        "turn_id": "turn_demo_01",
        "provider_call_id": "provider_call_demo_01",
        "stream_sequence": 1,
        "event_type": "message.end",
        "occurred_at": "2026-08-20T10:00:00Z",
        "data": {"stop_reason": "stop"},
        "error": None,
    }
    assert ModelEvent.model_validate_json(json.dumps(base)).event_type == "message.end"

    bad_sequence = {**base, "stream_sequence": 0}
    with pytest.raises(ValidationError):
        ModelEvent.model_validate_json(json.dumps(bad_sequence))

    bad_stop = {**base, "data": {"stop_reason": "thinking"}}
    with pytest.raises(ValidationError):
        ModelEvent.model_validate_json(json.dumps(bad_stop))

    thinking = {
        **base,
        "event_type": "text.delta",
        "data": {"text": "ok", "thinking": "secret reasoning"},
    }
    with pytest.raises(ValidationError):
        ModelEvent.model_validate_json(json.dumps(thinking))


def test_event_golden_vectors_are_valid_json() -> None:
    for name in ("text_events.ndjson", "tool_events.ndjson"):
        lines = (GOLDEN / name).read_text(encoding="utf-8").splitlines()
        assert lines
        for line in lines:
            assert ModelEvent.model_validate_json(line).stream_sequence >= 1
