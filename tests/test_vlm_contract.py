from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from server.vlm import VLMDecisionRequest, VLMObservation, validate_observation


NOW = datetime(2026, 7, 23, 10, 0, tzinfo=UTC)


def make_request() -> VLMDecisionRequest:
    return VLMDecisionRequest.create(
        decision_id="dec_1",
        session_id="ses_1",
        step_id="step_03",
        context_version=7,
        frame_id="frm_9",
        requested_at=NOW,
        completion_check="锅中没有明显饭块，配料分布均匀。",
        expected_objects=("rice", "wok", "spatula"),
    )


def make_observation(**updates) -> VLMObservation:
    data = {
        "decision_id": "dec_1",
        "step_id": "step_03",
        "context_version": 7,
        "frame_id": "frm_9",
        "phase": "likely_complete",
        "confidence": 0.81,
        "observed_objects": ["rice", "wok"],
        "risk_level": "none",
        "reason": "米饭已散开且分布均匀。",
    }
    data.update(updates)
    return VLMObservation.model_validate(data)


def test_matching_response_within_ttl_is_accepted_and_becomes_evidence() -> None:
    result = validate_observation(
        make_request(), make_observation(), received_at=NOW + timedelta(seconds=2)
    )
    assert result.status == "accepted"
    event = result.to_event(seq=10, t_device_ms=9_000, source="gemini_vlm_v1")
    assert event.type == "vlm.step_assessment"
    assert event.payload["phase"] == "likely_complete"
    assert event.payload["validation_status"] == "accepted"
    assert not event.backfill


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"decision_id": "dec_old"}, "decision_mismatch"),
        ({"step_id": "step_02"}, "step_mismatch"),
        ({"context_version": 6}, "context_version_mismatch"),
        ({"frame_id": "frm_old"}, "frame_mismatch"),
    ],
)
def test_identifier_mismatch_is_stale(updates, reason) -> None:
    result = validate_observation(
        make_request(),
        make_observation(**updates),
        received_at=NOW + timedelta(seconds=2),
    )
    assert result.status == "stale"
    assert result.stale_reason == reason
    event = result.to_event(seq=11, t_device_ms=9_000, source="gemini_vlm_v1")
    assert event.type == "vlm.step_assessment.stale"
    assert not event.backfill


def test_result_after_eight_second_ttl_is_stale_even_when_ids_match() -> None:
    result = validate_observation(
        make_request(), make_observation(), received_at=NOW + timedelta(seconds=8.001)
    )
    assert result.status == "stale"
    assert result.stale_reason == "ttl_expired"
    event = result.to_event(seq=11, t_device_ms=9_000, source="gemini_vlm_v1")
    assert event.type == "vlm.step_assessment.stale"
    assert event.backfill
    assert event.payload["stale_reason"] == "ttl_expired"
