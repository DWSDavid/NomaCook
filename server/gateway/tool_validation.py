"""Restricted JSON Schema and final Tool argument validation."""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from .contracts import APPROVED_TOOLS, MAX_TOOL_SCHEMA_BYTES, ModelTool


MAX_ARGUMENT_BYTES = MAX_TOOL_SCHEMA_BYTES


class ToolArgumentsError(ValueError):
    """A provider Tool proposal is not legal for the request."""


def validate_tool_arguments(
    *,
    tool_name: str,
    arguments_json: str,
    tools: tuple[ModelTool, ...],
) -> dict[str, Any]:
    if tool_name not in APPROVED_TOOLS:
        raise ToolArgumentsError("unknown Tool")
    if len(arguments_json.encode("utf-8")) > MAX_ARGUMENT_BYTES:
        raise ToolArgumentsError("Tool arguments exceed 32 KiB")
    selected = next((tool for tool in tools if tool.name == tool_name), None)
    if selected is None:
        raise ToolArgumentsError("Tool is not in request allowlist")
    try:
        arguments = _strict_json(arguments_json)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        del exc
        raise ToolArgumentsError("Tool arguments are not valid JSON") from None
    if not isinstance(arguments, dict):
        raise ToolArgumentsError("Tool arguments must be a JSON object")

    schema = selected.parameters
    try:
        _validate_schema_shape(schema)
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(arguments)
    except (SchemaError, ValidationError, ValueError) as exc:
        del exc
        raise ToolArgumentsError("Tool arguments do not match the request schema") from None
    return arguments


def _strict_json(value: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, child in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = child
        return result

    return json.loads(value, object_pairs_hook=pairs)


def _validate_schema_shape(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"$ref", "$dynamicRef"}:
                raise ValueError("schema references are forbidden")
            if key == "$schema" and child != "https://json-schema.org/draft/2020-12/schema":
                raise ValueError("unsupported schema draft")
            _validate_schema_shape(child)
    elif isinstance(value, list):
        for child in value:
            _validate_schema_shape(child)
