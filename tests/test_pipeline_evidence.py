from __future__ import annotations

from dataclasses import dataclass

from perception.fusion import InteractionEvent
from server.pipeline.evidence import (
    interaction_event,
    objects_present_event,
    presence_states,
    scripted_event,
)


@dataclass(frozen=True)
class FakeDet:
    canonical_label: str
    conf: float


def _interaction() -> InteractionEvent:
    return InteractionEvent(
        t=1.5,
        frame=45,
        event="hand_holding_object",
        hand="Right",
        object="bowl",
        conf=0.62,
        hand_box=(0, 0, 10, 10),
        obj_box=(2, 2, 12, 12),
    )


def test_interaction_event_is_deterministic_and_valid():
    a = interaction_event(_interaction(), session_id="ses_x", seq=3)
    b = interaction_event(_interaction(), session_id="ses_x", seq=3)
    assert a.canonical_dict() == b.canonical_dict()
    assert a.event_id == "evt_ses_x_00000003"
    assert a.type == "perception.hand_object_relation"
    assert a.t_device_ms == 1500.0
    assert a.payload["relation"] == "holding"
    assert a.payload["phase"] == "start"
    assert a.payload["hand"] == "right"
    assert a.payload["object_class"] == "bowl"


def test_end_event_maps_to_phase_end():
    ev = _interaction()
    end = InteractionEvent(**{**ev.__dict__, "event": "hand_holding_object_end"})
    env = interaction_event(end, session_id="ses_x", seq=4)
    assert env.payload["phase"] == "end"


def test_presence_states_require_all_objects_and_use_min_conf():
    dets = [FakeDet("tomato", 0.8), FakeDet("egg", 0.7), FakeDet("bowl", 0.66)]
    assert presence_states("step_01_prepare", dets) == [
        ("tomato_egg_tools_ready", 0.66)
    ]
    assert presence_states("step_01_prepare", dets[:2]) == []
    assert presence_states("step_02_scramble_egg", dets) == []


def test_objects_present_event_matches_sop_payload_contract():
    env = objects_present_event(
        "tomato_egg_tools_ready",
        0.66,
        session_id="ses_x",
        seq=5,
        step_id="step_01_prepare",
        pts_ms=3000.0,
        frame_idx=90,
    )
    assert env.type == "perception.objects_present"
    assert env.payload == {
        "step_id": "step_01_prepare",
        "state": "tomato_egg_tools_ready",
    }
    assert env.confidence == 0.66
    assert env.frame_id == "frame_000090"


def test_scripted_vlm_and_confirmation_events():
    vlm = scripted_event(
        {
            "pts_ms": 400,
            "type": "vlm.step_assessment",
            "step_id": "step_01_prepare",
            "phase": "likely_complete",
            "confidence": 0.85,
        },
        index=0,
        session_id="ses_x",
        seq=6,
        question_event_id=None,
    )
    assert vlm.type == "vlm.step_assessment"
    assert vlm.payload["phase"] == "likely_complete"
    assert vlm.confidence == 0.85

    ok = scripted_event(
        {
            "pts_ms": 600,
            "type": "voice.user_confirmation",
            "step_id": "step_02_scramble_egg",
        },
        index=1,
        session_id="ses_x",
        seq=7,
        question_event_id="evt_ses_x_00000002",
    )
    assert ok.payload["confirmed"] is True
    assert ok.payload["transcript_event_id"] == "script_line_1"
    assert ok.payload["question_event_id"] == "evt_ses_x_00000002"
    assert ok.confidence == 0.95
