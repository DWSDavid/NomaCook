from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json

import pytest
from pydantic import ValidationError

from server.events import (
    AudioRange,
    DuplicateEventConflict,
    EventEnvelope,
    EventLog,
    EvidencePayload,
    create_event,
)
from server.events.log import EventLogCorruption, read_events
from server.events.replay import compare_logs, replay


BASE_TIME = datetime(2026, 7, 23, 8, 22, tzinfo=UTC)


def make_event(
    *, seq: int = 1, event_id: str = "evt_fixed", received_offset: int = 0
) -> EventEnvelope:
    payload = EvidencePayload(
        hand="right",
        relation="holding",
        object_class="bottle",
        relation_confidence=0.72,
        phase_confidence=0.61,
        signals={"grip_closure": 0.8, "box_overlap": 0.4},
    )
    return create_event(
        event_id=event_id,
        session_id="ses_fixed",
        seq=seq,
        event_type="perception.hand_object_relation",
        t_device_ms=391_884 + seq,
        t_server_est=BASE_TIME + timedelta(milliseconds=seq),
        received_at=BASE_TIME + timedelta(seconds=received_offset),
        frame_id=f"frm_{seq:05d}",
        source="mediapipe_geometry_v1",
        confidence=0.72,
        payload=payload,
    )


def test_schema_requires_aware_timestamps_and_valid_audio_range() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        create_event(
            event_id="evt_naive",
            session_id="ses_fixed",
            seq=1,
            event_type="voice.transcript",
            t_device_ms=10,
            t_server_est=datetime(2026, 7, 23),
            source="gemini_live",
            payload={},
        )

    with pytest.raises(ValidationError, match="end_sample"):
        AudioRange(start_sample=200, end_sample=100, sample_rate_hz=16_000)


def test_event_log_is_idempotent_but_rejects_conflicting_reuse(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    log = EventLog(path)
    original = make_event()

    assert log.append(original) is True
    assert log.append(original.model_copy(update={"received_at": BASE_TIME + timedelta(seconds=9)})) is False
    assert len(read_events(path)) == 1

    changed = original.model_copy(update={"payload": {"relation": "near"}})
    with pytest.raises(DuplicateEventConflict):
        log.append(changed)


def test_replay_orders_backfill_by_seq_not_append_order(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    log = EventLog(path)
    assert log.append(make_event(seq=2, event_id="evt_2"))
    assert log.append(
        make_event(seq=1, event_id="evt_1").model_copy(update={"backfill": True})
    )

    assert [event.seq for event in replay(read_events(path))] == [1, 2]


def test_compare_ignores_only_received_at(tmp_path) -> None:
    left = tmp_path / "left.jsonl"
    right = tmp_path / "right.jsonl"
    EventLog(left).append(make_event(received_offset=0))
    EventLog(right).append(make_event(received_offset=5))

    assert compare_logs(left, right).equal
    assert not compare_logs(left, right, ignore_received_at=False).equal


def test_duplicate_seq_and_malformed_rows_are_rejected(tmp_path) -> None:
    with pytest.raises(ValueError, match="duplicate seq"):
        replay([make_event(event_id="evt_a"), make_event(event_id="evt_b")])

    bad_path = tmp_path / "bad.jsonl"
    bad_path.write_text(json.dumps({"event_id": "evt_incomplete"}) + "\n")
    with pytest.raises(EventLogCorruption, match="invalid event envelope"):
        read_events(bad_path)
