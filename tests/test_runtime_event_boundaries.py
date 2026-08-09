from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from server.engine import StateEngine, load_recipe
from server.events import create_event
from server.events.schema import EventEnvelope


BASE_TIME = datetime(2026, 7, 23, 9, 0, tzinfo=UTC)
REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent


def _event(**kwargs):
    defaults = {
        "event_id": "evt_1",
        "session_id": "session_1",
        "seq": 1,
        "type": "OBJECT_PRESENT",
        "t_device_ms": 100.0,
        "t_server_est": BASE_TIME,
        "received_at": BASE_TIME,
        "source": "fixture",
        "confidence": 0.9,
        "payload": {"object": "tomato"},
    }
    defaults.update(kwargs)
    return EventEnvelope(**defaults)


def test_event_defaults_to_run_and_null_context_version() -> None:
    event = _event()
    assert event.runtime_mode == "RUN"
    assert event.context_version is None


def test_explicit_context_version_is_preserved() -> None:
    event = _event(context_version=7)
    assert event.context_version == 7
    assert event.runtime_mode == "RUN"


def test_context_version_zero_is_rejected() -> None:
    with pytest.raises(ValidationError, match="context_version"):
        _event(context_version=0)


def test_runtime_mode_shadow_is_accepted() -> None:
    event = _event(runtime_mode="SHADOW")
    assert event.runtime_mode == "SHADOW"


def test_runtime_mode_replay_eval_is_accepted() -> None:
    event = _event(runtime_mode="REPLAY_EVAL")
    assert event.runtime_mode == "REPLAY_EVAL"


def test_invalid_runtime_mode_is_rejected() -> None:
    with pytest.raises(ValidationError, match="runtime_mode"):
        _event(runtime_mode="LIVE")


def test_create_event_passes_context_version_and_runtime_mode() -> None:
    event = create_event(
        session_id="ses_test",
        seq=2,
        event_type="OBJECT_PRESENT",
        t_device_ms=200.0,
        t_server_est=BASE_TIME,
        source="test",
        payload={"object": "tomato"},
        context_version=5,
        runtime_mode="REPLAY_EVAL",
    )
    assert event.context_version == 5
    assert event.runtime_mode == "REPLAY_EVAL"


def test_create_event_defaults_to_null_context_and_run() -> None:
    event = create_event(
        session_id="ses_test",
        seq=3,
        event_type="OBJECT_PRESENT",
        t_device_ms=300.0,
        t_server_est=BASE_TIME,
        source="test",
        payload={"object": "tomato"},
    )
    assert event.context_version is None
    assert event.runtime_mode == "RUN"


def test_evidence_factory_keeps_null_context_for_fast_cv() -> None:
    from server.pipeline.evidence import interaction_event
    from perception.fusion import InteractionEvent

    ie = InteractionEvent(
        t=1.0,
        frame=30,
        event="hand_holding_object",
        hand="Right",
        object="tomato",
        conf=0.72,
        hand_box=(0, 0, 10, 10),
        obj_box=(2, 2, 12, 12),
    )
    env = interaction_event(ie, session_id="ses_x", seq=1)
    assert env.context_version is None
    assert env.runtime_mode == "RUN"
