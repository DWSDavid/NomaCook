from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from server.vlm import VLMDecisionRequest, VLMObservation, validate_observation
from server.vlm.client import GeminiVLMClient, SYSTEM_PROMPT
from server.pipeline.evidence import scripted_event


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


def test_coach_comment_flows_into_event_payload():
    from datetime import UTC, datetime

    from server.vlm.schema import (
        VLMDecisionRequest, VLMObservation, validate_observation,
    )

    request = VLMDecisionRequest.create(
        decision_id="dec_x", session_id="ses_x", step_id="step_01_prepare",
        context_version=1, frame_id="frame_000001",
        requested_at=datetime(2026, 7, 24, tzinfo=UTC),
        completion_check="蛋液均匀", expected_objects=("bowl",),
    )
    observation = VLMObservation(
        decision_id="dec_x", step_id="step_01_prepare", context_version=1,
        frame_id="frame_000001", phase="in_progress", confidence=0.7,
        reason="碗里还有整颗蛋黄", coach_comment="可以顺手把葱先切了",
    )
    validated = validate_observation(
        request, observation, received_at=datetime(2026, 7, 24, tzinfo=UTC))
    event = validated.to_event(seq=1, t_device_ms=1000.0, source="test")
    assert event.payload["coach_comment"] == "可以顺手把葱先切了"
    # omission still validates (None default) — old responses stay compatible
    older = VLMObservation(
        decision_id="dec_x", step_id="step_01_prepare", context_version=1,
        frame_id="frame_000001", phase="in_progress", confidence=0.7,
        reason="碗里还有整颗蛋黄",
    )
    assert older.coach_comment is None


def test_gemini_prompt_appends_detector_context_as_non_authoritative_hint() -> None:
    request = make_request().model_copy(
        update={
            "detection_context": (
                "以下是本地感知的粗略估计（仅供参考）：\n"
                "- 手部：右手拿着木铲\n- 位置：炒锅在画面中间"
            )
        }
    )

    class CaptureModels:
        kwargs = None

        def generate_content(self, **kwargs):
            self.kwargs = kwargs
            return SimpleNamespace(
                text=json.dumps(make_observation().model_dump(mode="json"))
            )

    models = CaptureModels()
    client = object.__new__(GeminiVLMClient)
    client.model = "test-model"
    client.attempts = 1
    client._client = SimpleNamespace(models=models)

    observation = client.analyze_image(request, b"jpeg")

    assert observation.reason == "米饭已散开且分布均匀。"
    prompt = models.kwargs["contents"][0]
    assert request.detection_context in prompt
    assert "只能当提示，绝不能当结论" in SYSTEM_PROMPT
    assert "冲突时相信画面" in SYSTEM_PROMPT


# ── Fix 4: context_version propagation ──


def test_vlm_to_event_passes_context_version() -> None:
    result = validate_observation(
        make_request(), make_observation(), received_at=NOW + timedelta(seconds=2)
    )
    event = result.to_event(seq=10, t_device_ms=9_000, source="gemini_vlm_v1")
    assert event.context_version == 7


def test_scripted_vlm_event_passes_context_version() -> None:
    event = scripted_event(
        {
            "pts_ms": 400,
            "type": "vlm.step_assessment",
            "step_id": "step_03",
            "phase": "likely_complete",
            "confidence": 0.85,
        },
        index=0,
        session_id="ses_x",
        seq=6,
        question_event_id=None,
        context_version=12,
    )
    assert event.context_version == 12
    assert event.type == "vlm.step_assessment"


def test_scripted_voice_event_passes_context_version() -> None:
    event = scripted_event(
        {
            "pts_ms": 600,
            "type": "voice.user_confirmation",
            "step_id": "step_03",
        },
        index=1,
        session_id="ses_x",
        seq=7,
        question_event_id="evt_q",
        context_version=8,
    )
    assert event.context_version == 8
    assert event.type == "voice.user_confirmation"


def test_scripted_event_context_version_defaults_to_none() -> None:
    event = scripted_event(
        {
            "pts_ms": 400,
            "type": "vlm.step_assessment",
            "step_id": "step_01",
        },
        index=0,
        session_id="ses_x",
        seq=5,
        question_event_id=None,
    )
    assert event.context_version is None
