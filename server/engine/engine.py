"""Single-writer evidence accumulator and deterministic step transition engine."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from server.events import EventEnvelope

from .models import (
    EvidenceReference,
    PendingQuestion,
    SessionContext,
    StepProgress,
    StepTransition,
)
from .sop import EvidenceRule, RecipeSOP, RecipeStep


MAX_FRAME_AGE_MS = 3_000.0
RECENT_EVIDENCE_LIMIT = 32


class StateEngineError(RuntimeError):
    """Base state-engine error."""


class OutOfOrderEvent(StateEngineError):
    """The single writer received an event behind the committed sequence."""


class SessionMismatch(StateEngineError):
    """An event belongs to a different session."""


class EngineResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal[
        "duplicate",
        "stale",
        "unmatched",
        "evidence_added",
        "question_pending",
        "step_completed",
        "session_completed",
    ]
    context: SessionContext
    transition: StepTransition | None = None


def _payload_value(payload: dict[str, Any], dotted_path: str) -> Any:
    value: Any = payload
    for key in dotted_path.split("."):
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def _matches(event: EventEnvelope, rule: EvidenceRule) -> bool:
    if event.type != rule.event_type:
        return False
    if (event.confidence or 0.0) < rule.min_confidence:
        return False
    return all(
        _payload_value(event.payload, path) == expected
        for path, expected in rule.payload_matches.items()
    )


def _valid_confirmation(event: EventEnvelope, step: RecipeStep) -> bool:
    if event.type != "voice.user_confirmation":
        return True
    if event.payload.get("step_id") != step.id:
        return False
    if event.payload.get("confirmed") is not True:
        return False
    if not event.payload.get("transcript_event_id"):
        return False
    if step.high_risk and not event.payload.get("question_event_id"):
        return False
    return True


class StateEngine:
    """Consume one session's events in strict seq order and own its context."""

    def __init__(
        self,
        *,
        session_id: str,
        recipe: RecipeSOP,
        started_at: datetime,
        user_preferences: dict[str, str] | None = None,
        safety_constraints: tuple[str, ...] = (),
    ) -> None:
        if started_at.tzinfo is None or started_at.utcoffset() is None:
            raise ValueError("started_at must include a timezone")
        self.recipe = recipe
        self._step_index = 0
        self._seen_event_ids: set[str] = set()
        first_step = recipe.steps[0]
        self._context = SessionContext(
            session_id=session_id,
            recipe_version_id=recipe.recipe_version_id,
            current_step_id=first_step.id,
            started_at=started_at.astimezone(UTC),
            active_objects=first_step.objects_involved,
            user_preferences=user_preferences
            or {"language": recipe.language, "verbosity": "short"},
            safety_constraints=safety_constraints,
        )

    @property
    def context(self) -> SessionContext:
        return self._context

    @property
    def current_step(self) -> RecipeStep:
        return self.recipe.steps[self._step_index]

    def consume(self, event: EventEnvelope) -> EngineResult:
        if event.session_id != self._context.session_id:
            raise SessionMismatch(
                f"event session {event.session_id!r} != {self._context.session_id!r}"
            )
        if event.event_id in self._seen_event_ids:
            return EngineResult(status="duplicate", context=self._context)
        if event.seq <= self._context.last_seq:
            raise OutOfOrderEvent(
                f"event seq {event.seq} is not after committed seq {self._context.last_seq}"
            )

        self._seen_event_ids.add(event.event_id)
        if self._context.step_status == "completed":
            self._context = self._context.model_copy(
                update={
                    "last_seq": event.seq,
                    "context_version": self._context.context_version + 1,
                }
            )
            return EngineResult(status="session_completed", context=self._context)

        frame_age_ms = max(
            0.0, (event.received_at - event.t_server_est).total_seconds() * 1000.0
        )
        if frame_age_ms > MAX_FRAME_AGE_MS:
            reference = EvidenceReference(
                event_id=event.event_id,
                seq=event.seq,
                event_type=event.type,
                stale=True,
                reason=f"frame_age_ms={frame_age_ms:.1f}",
            )
            self._update_context(
                event,
                recent_reference=reference,
                progress=self._context.step_progress,
                pending_question=self._context.pending_question,
            )
            return EngineResult(status="stale", context=self._context)

        step = self.current_step
        rules = (
            [rule for rule in step.completion_policy.evidence_rules if _matches(event, rule)]
            if _valid_confirmation(event, step)
            else []
        )
        progress = self._context.step_progress
        matched_ids = set(progress.matched_rule_ids)
        refs = list(progress.evidence_refs)
        score = progress.score

        for rule in rules:
            weight_added = 0.0
            if rule.id not in matched_ids:
                matched_ids.add(rule.id)
                weight_added = rule.weight
                score = min(1.0, score + weight_added)
            refs.append(
                EvidenceReference(
                    event_id=event.event_id,
                    seq=event.seq,
                    event_type=event.type,
                    rule_id=rule.id,
                    weight_added=weight_added,
                )
            )

        consecutive_hits = progress.consecutive_hits
        uncertain_since = progress.uncertain_since
        if rules:
            if score >= step.completion_policy.threshold:
                consecutive_hits += 1
            else:
                consecutive_hits = 0
            if (
                score >= step.completion_policy.question_min_score
                and score < step.completion_policy.threshold
                and uncertain_since is None
            ):
                uncertain_since = event.t_server_est

        new_progress = StepProgress(
            score=score,
            consecutive_hits=consecutive_hits,
            matched_rule_ids=tuple(sorted(matched_ids)),
            evidence_refs=tuple(refs),
            uncertain_since=uncertain_since,
        )

        if (
            rules
            and score >= step.completion_policy.threshold
            and consecutive_hits >= step.completion_policy.consecutive_hits
        ):
            return self._complete_step(event, new_progress)

        previous_pending = self._context.pending_question
        pending = previous_pending
        if (
            pending is None
            and uncertain_since is not None
            and score < step.completion_policy.threshold
            and (event.t_server_est - uncertain_since).total_seconds() * 1000
            >= step.completion_policy.question_after_ms
        ):
            pending = PendingQuestion(
                step_id=step.id,
                question=step.completion_policy.question,
                triggered_by_event_id=event.event_id,
                score=score,
            )
        question_created = pending is not None and previous_pending is None

        recent_reference = refs[-1] if rules else None
        self._update_context(
            event,
            recent_reference=recent_reference,
            progress=new_progress,
            pending_question=pending,
        )
        if question_created:
            return EngineResult(status="question_pending", context=self._context)
        if rules:
            return EngineResult(status="evidence_added", context=self._context)
        if pending is not None:
            return EngineResult(status="question_pending", context=self._context)
        return EngineResult(status="unmatched", context=self._context)

    def _update_context(
        self,
        event: EventEnvelope,
        *,
        recent_reference: EvidenceReference | None,
        progress: StepProgress,
        pending_question: PendingQuestion | None = None,
    ) -> None:
        recent = list(self._context.recent_evidence)
        if recent_reference is not None:
            recent.append(recent_reference)
            recent = recent[-RECENT_EVIDENCE_LIMIT:]
        self._context = self._context.model_copy(
            update={
                "last_seq": event.seq,
                "step_progress": progress,
                "recent_evidence": tuple(recent),
                "pending_question": pending_question,
                "context_version": self._context.context_version + 1,
            }
        )

    def _complete_step(
        self, event: EventEnvelope, progress: StepProgress
    ) -> EngineResult:
        completed_step = self.current_step
        next_index = self._step_index + 1
        next_step = (
            self.recipe.steps[next_index] if next_index < len(self.recipe.steps) else None
        )
        transition = StepTransition(
            decision_id=f"dec_{self._context.session_id}_{event.seq}",
            completed_step_id=completed_step.id,
            next_step_id=next_step.id if next_step else None,
            evidence_refs=tuple(ref.event_id for ref in progress.evidence_refs),
            score=progress.score,
            decided_at=event.t_server_est,
        )
        recent = list(self._context.recent_evidence)
        if progress.evidence_refs:
            recent.append(progress.evidence_refs[-1])
        recent = recent[-RECENT_EVIDENCE_LIMIT:]

        if next_step is None:
            self._context = self._context.model_copy(
                update={
                    "step_status": "completed",
                    "last_seq": event.seq,
                    "step_progress": progress,
                    "recent_evidence": tuple(recent),
                    "pending_question": None,
                    "active_objects": (),
                    "context_version": self._context.context_version + 1,
                }
            )
            return EngineResult(
                status="session_completed",
                context=self._context,
                transition=transition,
            )

        self._step_index = next_index
        self._context = self._context.model_copy(
            update={
                "current_step_id": next_step.id,
                "last_seq": event.seq,
                "step_progress": StepProgress(),
                "recent_evidence": tuple(recent),
                "pending_question": None,
                "active_objects": next_step.objects_involved,
                "context_version": self._context.context_version + 1,
            }
        )
        return EngineResult(
            status="step_completed", context=self._context, transition=transition
        )
