"""Unified event contracts and append-only storage for NomaChef."""

from .log import DuplicateEventConflict, EventLog, EventLogCorruption
from .schema import (
    EVENT_SCHEMA_VERSION,
    AudioRange,
    EventEnvelope,
    EvidencePayload,
    create_event,
)

__all__ = [
    "EVENT_SCHEMA_VERSION",
    "AudioRange",
    "DuplicateEventConflict",
    "EventEnvelope",
    "EventLog",
    "EventLogCorruption",
    "EvidencePayload",
    "create_event",
]
