from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from server.engine import StateEngine, load_recipe
from server.engine.engine import _completion_ready
from server.engine.models import PendingQuestion
from server.engine.sop import CompletionPolicy, EvidenceRule
from server.events import create_event


BASE_TIME = datetime(2026, 7, 23, 9, 0, tzinfo=UTC)
REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent


def _load_tomato_recipe():
    return load_recipe(REPO_ROOT / "sop" / "tomato_to_fridge.json")


def _make_event(
    *,
    seq: int,
    event_type: str,
    payload: dict | None = None,
    confidence: float = 0.9,
    context_version: int | None = None,
    runtime_mode: str = "RUN",
    at_ms: int = 0,
) -> create_event:
    event_time = BASE_TIME + timedelta(milliseconds=at_ms)
    return create_event(
        event_id=f"evt_rec_{seq}",
        session_id="ses_recovery",
        seq=seq,
        event_type=event_type,
        t_device_ms=float(at_ms),
        t_server_est=event_time,
        received_at=event_time,
        source="test",
        confidence=confidence,
        payload=payload or {},
        context_version=context_version,
        runtime_mode=runtime_mode,
    )


def _make_engine(step_index: int = 0, started_at: datetime | None = None):
    recipe = _load_tomato_recipe()
    engine = StateEngine(
        session_id="ses_recovery",
        recipe=recipe,
        started_at=started_at or BASE_TIME,
    )
    engine._step_index = step_index
    first_step = recipe.steps[step_index]
    engine._context = engine._context.model_copy(
        update={
            "current_step_id": first_step.id,
            "active_objects": first_step.objects_involved,
            "step_progress": engine._context.step_progress,
        }
    )
    return engine


def test_shadow_event_never_changes_live_context() -> None:
    recipe = _load_tomato_recipe()
    engine = StateEngine(
        session_id="ses_recovery", recipe=recipe, started_at=BASE_TIME
    )
    before = engine.context
    event = _make_event(
        seq=1,
        event_type="OBJECT_PRESENT",
        payload={"object": "tomato"},
        runtime_mode="SHADOW",
        context_version=before.context_version,
    )
    result = engine.consume(event)
    assert result.status == "shadow_ignored"
    assert engine.context == before


def test_stale_context_event_cannot_advance() -> None:
    recipe = _load_tomato_recipe()
    engine = StateEngine(
        session_id="ses_recovery", recipe=recipe, started_at=BASE_TIME
    )
    current = engine.context
    event = _make_event(
        seq=1,
        event_type="OBJECT_PRESENT",
        payload={"object": "tomato"},
        runtime_mode="RUN",
        context_version=current.context_version + 1,
    )
    result = engine.consume(event)
    assert result.status == "context_mismatch"
    assert engine.context == current


def test_null_context_fast_cv_event_still_accepted() -> None:
    engine = _make_engine()
    event = _make_event(
        seq=1,
        event_type="OBJECT_PRESENT",
        payload={"object": "tomato"},
        context_version=None,
    )
    result = engine.consume(event)
    assert result.status == "evidence_added"
    assert engine.context.step_progress.score == pytest.approx(0.4)


def test_recovery_from_held_to_table() -> None:
    engine = _make_engine(step_index=3)
    assert engine.current_step.id == "tomato_held"

    result = engine.consume(
        _make_event(
            seq=1,
            event_type="OBJECT_RETURNED_TO_REGION",
            payload={"object": "tomato", "region": "table"},
            context_version=engine.context.context_version,
        )
    )
    assert result.status == "recovered"
    assert engine.context.current_step_id == "tomato_on_table"
    assert engine.context.step_progress.score == 0.0
    assert engine.context.step_progress.consecutive_hits == 0


def test_recovery_clears_pending_question() -> None:
    engine = _make_engine(step_index=3)
    ctx = engine.context
    engine._context = ctx.model_copy(
        update={
            "pending_question": PendingQuestion(
                step_id="tomato_held",
                question="你还拿着番茄吗？",
                triggered_by_event_id="evt_x",
                score=0.4,
            )
        }
    )
    result = engine.consume(
        _make_event(
            seq=1,
            event_type="OBJECT_RETURNED_TO_REGION",
            payload={"object": "tomato", "region": "table"},
            context_version=engine.context.context_version,
        )
    )
    assert result.status == "recovered"
    assert engine.context.pending_question is None


def test_one_source_group_cannot_satisfy_two_source_policy() -> None:
    rules = (
        EvidenceRule(
            id="hand_a",
            event_type="HAND_A",
            weight=0.4,
            min_confidence=0.8,
            source_group="hand_relation",
        ),
        EvidenceRule(
            id="hand_b",
            event_type="HAND_B",
            weight=0.4,
            min_confidence=0.8,
            source_group="hand_relation",
        ),
        EvidenceRule(
            id="stable_inside",
            event_type="STABLE_INSIDE",
            weight=0.2,
            min_confidence=0.8,
            source_group="region_stability",
        ),
    )
    policy = CompletionPolicy(
        threshold=0.8,
        consecutive_hits=2,
        question_min_score=0.4,
        question_after_ms=3000,
        question="需要更多证据吗？",
        evidence_rules=rules,
        min_source_groups=2,
        evidence_window_ms=3000,
    )

    assert not _completion_ready(
        score=0.8,
        consecutive_hits=2,
        matched_source_groups={"hand_relation"},
        policy=policy,
    )
    assert _completion_ready(
        score=1.0,
        consecutive_hits=2,
        matched_source_groups={"hand_relation", "region_stability"},
        policy=policy,
    )


def test_evidence_outside_window_does_not_accumulate() -> None:
    engine = _make_engine(step_index=7)
    assert engine.current_step.id == "tomato_released_inside"

    first = _make_event(
        seq=1,
        event_type="VISIBILITY_LOST",
        payload={"object": "tomato"},
        confidence=0.95,
        at_ms=1000,
    )
    late = _make_event(
        seq=2,
        event_type="OBJECT_STABLE_IN_REGION",
        payload={"object": "tomato", "region": "refrigerator_interior"},
        confidence=0.95,
        at_ms=5500,
    )

    result1 = engine.consume(first)
    assert result1.context.step_progress.score == pytest.approx(0.4)
    result2 = engine.consume(late)
    assert result2.context.step_progress.score == pytest.approx(0.8)
    assert result2.status != "session_completed"
