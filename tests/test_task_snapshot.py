from __future__ import annotations

from datetime import UTC, datetime, timedelta

from server.engine import StateEngine, load_recipe
from server.engine.snapshot import TaskSnapshot, build_task_snapshot
from server.events import create_event


BASE_TIME = datetime(2026, 7, 23, 9, 0, tzinfo=UTC)
REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent


def _make_engine():
    recipe = load_recipe(REPO_ROOT / "sop" / "tomato_to_fridge.json")
    return StateEngine(
        session_id="ses_snap", recipe=recipe, started_at=BASE_TIME
    )


def test_snapshot_is_compact_and_contains_no_write_capability() -> None:
    engine = _make_engine()
    snapshot = build_task_snapshot(engine.context, engine.current_step)

    assert snapshot.session_id == engine.context.session_id
    assert snapshot.task_id == "tomato_to_fridge_v1"
    assert snapshot.state == "ready"
    assert snapshot.status in {"ON_TRACK", "UNCERTAIN", "COMPLETE"}
    assert snapshot.context_version == engine.context.context_version
    assert not hasattr(snapshot, "advance_step")
    assert "events" not in snapshot.model_dump()


def test_snapshot_json_round_trip() -> None:
    engine = _make_engine()
    original = build_task_snapshot(engine.context, engine.current_step)
    reloaded = TaskSnapshot.model_validate_json(original.model_dump_json())
    assert reloaded == original


def test_snapshot_updates_after_evidence() -> None:
    engine = _make_engine()
    engine.consume(
        create_event(
            event_id="evt_snap_1",
            session_id="ses_snap",
            seq=1,
            event_type="OBJECT_PRESENT",
            t_device_ms=100.0,
            t_server_est=BASE_TIME + timedelta(milliseconds=100),
            received_at=BASE_TIME + timedelta(milliseconds=100),
            source="test",
            confidence=0.9,
            payload={"object": "tomato"},
        )
    )
    snapshot = build_task_snapshot(engine.context, engine.current_step)
    assert snapshot.belief == 0.4
    assert snapshot.last_event_seq == 1


def test_snapshot_shows_missing_evidence_for_unmatched_rules() -> None:
    engine = _make_engine()
    snapshot = build_task_snapshot(engine.context, engine.current_step)
    assert "fridge_visible" in snapshot.missing_evidence
    assert "tomato_visible" in snapshot.missing_evidence

    engine.consume(
        create_event(
            event_id="evt_snap_2",
            session_id="ses_snap",
            seq=1,
            event_type="OBJECT_PRESENT",
            t_device_ms=200.0,
            t_server_est=BASE_TIME + timedelta(milliseconds=200),
            received_at=BASE_TIME + timedelta(milliseconds=200),
            source="test",
            confidence=0.9,
            payload={"object": "tomato"},
        )
    )
    snapshot = build_task_snapshot(engine.context, engine.current_step)
    assert "tomato_visible" not in snapshot.missing_evidence
    assert "fridge_visible" in snapshot.missing_evidence
