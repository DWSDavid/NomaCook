"""Fixed, sanitized service errors for the AI Model Service contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .contracts import ModelError


ErrorCode = Literal[
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


@dataclass(frozen=True)
class ModelServiceError:
    code: ErrorCode
    retryable: bool
    phase: str
    message: str
    retry_after_ms: int | None = None

    def to_model_error(self) -> ModelError:
        return ModelError(
            code=self.code,
            retryable=self.retryable,
            phase=self.phase,
            message=self.message,
            retry_after_ms=self.retry_after_ms,
        )


def safe_error(exception: BaseException) -> ModelServiceError:
    """Map an unknown exception to a constant message without leaking details."""

    del exception
    return ModelServiceError(
        code="AI_MODEL_SERVICE_ERROR",
        retryable=False,
        phase="service",
        message="internal model service error",
    )
