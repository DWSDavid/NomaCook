"""Versioned snapshots and decisions emitted by the state engine."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    seq: int
    event_type: str
    rule_id: str | None = None
    weight_added: float = 0.0
    stale: bool = False
    reason: str | None = None


class PendingQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str
    question: str
    triggered_by_event_id: str
    score: float = Field(ge=0.0, le=1.0)


class StepProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    score: float = Field(default=0.0, ge=0.0, le=1.0)
    consecutive_hits: int = Field(default=0, ge=0)
    matched_rule_ids: tuple[str, ...] = ()
    evidence_refs: tuple[EvidenceReference, ...] = ()
    uncertain_since: datetime | None = None


class SessionContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    recipe_version_id: str
    current_step_id: str
    step_status: Literal["in_progress", "completed"] = "in_progress"
    started_at: datetime
    recent_evidence: tuple[EvidenceReference, ...] = ()
    pending_question: PendingQuestion | None = None
    active_objects: tuple[str, ...] = ()
    user_preferences: dict[str, str] = Field(
        default_factory=lambda: {"language": "zh-CN", "verbosity": "short"}
    )
    safety_constraints: tuple[str, ...] = ()
    context_version: int = Field(default=1, ge=1)
    last_seq: int = -1
    step_progress: StepProgress = Field(default_factory=StepProgress)


class StepTransition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: str
    completed_step_id: str
    next_step_id: str | None
    evidence_refs: tuple[str, ...]
    score: float
    decided_at: datetime
