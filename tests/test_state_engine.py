from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from server.engine import OutOfOrderEvent, StateEngine, load_recipe
from server.engine.sop import RecipeSOP
from server.events import create_event


BASE_TIME = datetime(2026, 7, 23, 9, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parent.parent


def make_recipe(*, high_risk: bool = False) -> RecipeSOP:
    return RecipeSOP.model_validate(
        {
            "recipe_version_id": "rv_test_1",
            "dish": "test dish",
            "ingredients": [{"name": "rice", "amount": "1 bowl"}],
            "steps": [
                {
                    "id": "step_01",
                    "sequence": 1,
                    "instruction": "prepare",
                    "objects_involved": ["bottle", "wok"],
                    "completion_check": "seasoning is visibly mixed",
                    "est_duration_sec": 60,
                    "check_policy": "continuous_evidence",
                    "high_risk": high_risk,
                    "completion_policy": {
                        "threshold": 0.7,
                        "consecutive_hits": 2,
                        "question_min_score": 0.4,
                        "question_after_ms": 30_000,
                        "question": "is it ready?",
                        "evidence_rules": [
                            {
                                "id": "holding",
                                "event_type": "perception.hand_object_relation",
                                "payload_matches": {"step_id": "step_01", "relation": "holding"},
                                "weight": 0.4,
                                "min_confidence": 0.6,
                            },
                            {
                                "id": "roi",
                                "event_type": "perception.roi_change",
                                "payload_matches": {"step_id": "step_01", "change": "darker"},
                                "weight": 0.3,
                                "min_confidence": 0.6,
                            },
                            {
                                "id": "confirm",
                                "event_type": "voice.user_confirmation",
                                "payload_matches": {"step_id": "step_01", "confirmed": True},
                                "weight": 0.3,
                                "min_confidence": 0.9,
                            },
                        ],
                    },
                },
                {
                    "id": "step_02",
                    "sequence": 2,
                    "instruction": "finish",
                    "objects_involved": ["bowl"],
                    "completion_check": "food is visibly in the bowl",
                    "est_duration_sec": 30,
                    "check_policy": "visual_then_confirm",
                    "completion_policy": {
                        "threshold": 0.7,
                        "consecutive_hits": 2,
                        "question_min_score": 0.4,
                        "question_after_ms": 30_000,
                        "question": "is it plated?",
                        "evidence_rules": [
                            {
                                "id": "plated",
                                "event_type": "vlm.step_assessment",
                                "payload_matches": {"step_id": "step_02", "phase": "likely_complete"},
                                "weight": 0.8,
                                "min_confidence": 0.7,
                            }
                        ],
                    },
                },
            ],
        }
    )


def make_event(
    seq: int,
    event_type: str,
    payload: dict,
    *,
    confidence: float = 0.9,
    event_id: str | None = None,
    at_seconds: float | None = None,
    frame_age_ms: float = 0,
):
    event_time = BASE_TIME + timedelta(
        seconds=at_seconds if at_seconds is not None else seq
    )
    return create_event(
        event_id=event_id or f"evt_{seq}",
        session_id="ses_test",
        seq=seq,
        event_type=event_type,
        t_device_ms=seq * 100,
        t_server_est=event_time,
        received_at=event_time + timedelta(milliseconds=frame_age_ms),
        frame_id=f"frm_{seq}",
        source="test",
        confidence=confidence,
        payload=payload,
    )


def make_engine(*, high_risk: bool = False) -> StateEngine:
    return StateEngine(
        session_id="ses_test",
        recipe=make_recipe(high_risk=high_risk),
        started_at=BASE_TIME,
    )


def test_fried_rice_sop_loads_and_has_static_checks() -> None:
    recipe = load_recipe(REPO_ROOT / "sop" / "fried_rice.json")
    assert recipe.dish == "蛋炒饭"
    assert len(recipe.steps) == 5
    assert all(step.completion_check for step in recipe.steps)


def test_two_independent_signals_and_consecutive_hits_advance_step() -> None:
    engine = make_engine()
    holding = make_event(
        1,
        "perception.hand_object_relation",
        {"step_id": "step_01", "relation": "holding"},
    )
    roi = make_event(
        2,
        "perception.roi_change",
        {"step_id": "step_01", "change": "darker"},
    )
    roi_confirmation = make_event(
        3,
        "perception.roi_change",
        {"step_id": "step_01", "change": "darker"},
    )

    assert engine.consume(holding).context.step_progress.score == pytest.approx(0.4)
    first_threshold = engine.consume(roi)
    assert first_threshold.context.step_progress.score == pytest.approx(0.7)
    assert first_threshold.context.step_progress.consecutive_hits == 1

    completed = engine.consume(roi_confirmation)
    assert completed.status == "step_completed"
    assert completed.context.current_step_id == "step_02"
    assert completed.transition is not None
    assert completed.transition.evidence_refs == ("evt_1", "evt_2", "evt_3")


def test_low_confidence_stale_duplicate_and_out_of_order_do_not_advance() -> None:
    engine = make_engine()
    low = make_event(
        1,
        "perception.hand_object_relation",
        {"step_id": "step_01", "relation": "holding"},
        confidence=0.2,
    )
    stale = make_event(
        2,
        "perception.hand_object_relation",
        {"step_id": "step_01", "relation": "holding"},
        frame_age_ms=3_001,
    )

    assert engine.consume(low).status == "unmatched"
    assert engine.consume(stale).status == "stale"
    assert engine.context.step_progress.score == 0
    assert engine.consume(stale).status == "duplicate"

    with pytest.raises(OutOfOrderEvent):
        engine.consume(
            make_event(
                1,
                "perception.roi_change",
                {"step_id": "step_01", "change": "darker"},
                event_id="evt_late",
            )
        )


def test_intermediate_score_creates_question_after_deterministic_timeout() -> None:
    engine = make_engine()
    engine.consume(
        make_event(
            1,
            "perception.hand_object_relation",
            {"step_id": "step_01", "relation": "holding"},
            at_seconds=0,
        )
    )
    result = engine.consume(
        make_event(
            2,
            "perception.hand_object_relation",
            {"step_id": "step_01", "relation": "holding"},
            at_seconds=31,
        )
    )

    assert result.status == "question_pending"
    assert result.context.pending_question is not None
    assert result.context.pending_question.question == "is it ready?"
    assert result.context.pending_question.score == pytest.approx(0.4)


def test_voice_confirmation_requires_transcript_and_high_risk_question_binding() -> None:
    engine = make_engine(high_risk=True)
    missing_bindings = make_event(
        1,
        "voice.user_confirmation",
        {"step_id": "step_01", "confirmed": True},
    )
    transcript_only = make_event(
        2,
        "voice.user_confirmation",
        {
            "step_id": "step_01",
            "confirmed": True,
            "transcript_event_id": "evt_transcript",
        },
    )
    valid = make_event(
        3,
        "voice.user_confirmation",
        {
            "step_id": "step_01",
            "confirmed": True,
            "transcript_event_id": "evt_transcript",
            "question_event_id": "evt_question",
        },
    )

    assert engine.consume(missing_bindings).status == "unmatched"
    assert engine.consume(transcript_only).status == "unmatched"
    assert engine.consume(valid).context.step_progress.score == pytest.approx(0.3)


def test_final_step_marks_session_completed() -> None:
    engine = make_engine()
    events = [
        make_event(1, "perception.hand_object_relation", {"step_id": "step_01", "relation": "holding"}),
        make_event(2, "perception.roi_change", {"step_id": "step_01", "change": "darker"}),
        make_event(3, "perception.roi_change", {"step_id": "step_01", "change": "darker"}),
        make_event(4, "vlm.step_assessment", {"step_id": "step_02", "phase": "likely_complete"}),
        make_event(5, "vlm.step_assessment", {"step_id": "step_02", "phase": "likely_complete"}),
    ]
    result = None
    for event in events:
        result = engine.consume(event)

    assert result is not None
    assert result.status == "session_completed"
    assert result.context.step_status == "completed"
    assert result.transition is not None
    assert result.transition.completed_step_id == "step_02"
