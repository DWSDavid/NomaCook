from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.gateway.contracts import ModelRequest
from server.gateway.tool_validation import ToolArgumentsError, validate_tool_arguments


REPO = Path(__file__).resolve().parents[2]


def _request() -> ModelRequest:
    return ModelRequest.model_validate_json(
        (REPO / "server/gateway/contract/golden/request.json").read_text()
    )


def test_valid_arguments_are_returned_as_object() -> None:
    request = _request()
    result = validate_tool_arguments(
        tool_name="nomacook.speak@1",
        arguments_json='{"text":"hello"}',
        tools=request.tools,
    )
    assert result == {"text": "hello"}


@pytest.mark.parametrize(
    "arguments_json",
    ["", '{"text":', '{"text":"hello"} trailing', '["not-an-object"]'],
)
def test_incomplete_or_non_object_arguments_are_rejected(arguments_json: str) -> None:
    request = _request()
    with pytest.raises(ToolArgumentsError):
        validate_tool_arguments(
            tool_name="nomacook.speak@1",
            arguments_json=arguments_json,
            tools=request.tools,
        )


def test_unknown_tool_and_schema_mismatch_are_rejected() -> None:
    request = _request()
    with pytest.raises(ToolArgumentsError):
        validate_tool_arguments(
            tool_name="unknown.tool@1",
            arguments_json='{"text":"hello"}',
            tools=request.tools,
        )
    with pytest.raises(ToolArgumentsError):
        validate_tool_arguments(
            tool_name="nomacook.speak@1",
            arguments_json='{"text":"hello","extra":true}',
            tools=request.tools,
        )


def test_external_refs_and_oversized_arguments_are_rejected() -> None:
    request = _request()
    bad_schema = {
        "type": "object",
        "properties": {"text": {"$ref": "#/definitions/Text"}},
    }
    with pytest.raises(ToolArgumentsError):
        validate_tool_arguments(
            tool_name="nomacook.speak@1",
            arguments_json='{"text":"hello"}',
            tools=(request.tools[0].model_copy(update={"parameters": bad_schema}),),
        )

    with pytest.raises(ToolArgumentsError):
        validate_tool_arguments(
            tool_name="nomacook.speak@1",
            arguments_json=json.dumps({"text": "x" * (32 * 1024)}),
            tools=_request().tools,
        )
