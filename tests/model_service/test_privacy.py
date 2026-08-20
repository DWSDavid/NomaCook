from __future__ import annotations

from server.gateway.errors import ModelServiceError, safe_error


def test_unknown_exception_maps_to_fixed_safe_error() -> None:
    error = safe_error(Exception("Bearer secret-value"))
    assert error == ModelServiceError(
        code="AI_MODEL_SERVICE_ERROR",
        retryable=False,
        phase="service",
        message="internal model service error",
    )


def test_safe_error_message_is_constant_for_unknown_exceptions() -> None:
    left = safe_error(ValueError("first private payload"))
    right = safe_error(ValueError("second private payload"))
    assert left.message == right.message == "internal model service error"
