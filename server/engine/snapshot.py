"""Immutable, read-only projection of session state for downstream consumers."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from server.engine.models import SessionContext
from server.engine.sop import RecipeStep


class TaskSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    task_id: str
    task_goal: str = ""
    state: str
    step_title: str = ""
    step_instruction: str = ""
    status: Literal["ON_TRACK", "UNCERTAIN", "DEVIATING", "CRITICAL", "COMPLETE"]
    belief: float = Field(ge=0.0, le=1.0)
    active_objects: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    pending_question: str | None
    last_event_seq: int
    context_version: int


def build_task_snapshot(context: SessionContext, step: RecipeStep, task_goal: str = "") -> TaskSnapshot:
    matched = set(context.step_progress.matched_rule_ids)
    missing = [
        rule.id for rule in step.completion_policy.evidence_rules if rule.id not in matched
    ]
    if (
        context.step_progress.score >= step.completion_policy.threshold
        and context.step_progress.consecutive_hits
        < step.completion_policy.consecutive_hits
    ):
        missing.append("confirmation_streak")
    if context.step_status == "completed":
        status = "COMPLETE"
    elif (
        context.pending_question is not None
        or (
            context.step_progress.score > 0.0
            and (
                context.step_progress.score < step.completion_policy.threshold
                or context.step_progress.consecutive_hits
                < step.completion_policy.consecutive_hits
                or len(context.step_progress.matched_source_groups)
                < step.completion_policy.min_source_groups
            )
        )
    ):
        status = "UNCERTAIN"
    else:
        status = "ON_TRACK"
    return TaskSnapshot(
        session_id=context.session_id,
        task_id=context.recipe_version_id,
        task_goal=task_goal or context.recipe_version_id,
        state=context.current_step_id,
        step_title=step.title,
        step_instruction=step.instruction,
        status=status,
        belief=context.step_progress.score,
        active_objects=context.active_objects,
        missing_evidence=tuple(missing),
        pending_question=(
            context.pending_question.question if context.pending_question else None
        ),
        last_event_seq=context.last_seq,
        context_version=context.context_version,
    )
