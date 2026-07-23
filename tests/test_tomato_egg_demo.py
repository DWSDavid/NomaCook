from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np

from server.engine import StateEngine, load_recipe
from server.engine.models import SessionContext
from server.events import create_event
from server.perception import (
    build_detection_context,
    create_color_evidence_event,
    extract_tomato_egg_color_signals,
)


NOW = datetime(2026, 7, 23, 11, 0, tzinfo=UTC)


def solid_bgr(color: tuple[int, int, int]) -> np.ndarray:
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    frame[:, :] = color
    return frame


def event(seq: int, event_type: str, payload: dict, confidence: float = 0.9):
    at = NOW + timedelta(milliseconds=seq * 100)
    return create_event(
        event_id=f"evt_{seq}",
        session_id="ses_tomato_egg",
        seq=seq,
        event_type=event_type,
        t_device_ms=seq * 100,
        t_server_est=at,
        received_at=at,
        frame_id=f"frm_{seq}",
        source="test",
        confidence=confidence,
        payload=payload,
    )


def test_tomato_egg_is_the_simple_four_step_demo() -> None:
    recipe = load_recipe("sop/tomato_egg.json")
    assert recipe.dish == "番茄炒鸡蛋"
    assert [step.id for step in recipe.steps] == [
        "step_01_prepare",
        "step_02_scramble_egg",
        "step_03_soften_tomato",
        "step_04_combine_and_plate",
    ]

    first = recipe.steps[0]
    context = SessionContext(
        session_id="ses_tomato_egg",
        recipe_version_id=recipe.recipe_version_id,
        current_step_id=first.id,
        started_at=NOW,
        active_objects=first.objects_involved,
    )
    prompts = build_detection_context(context, recipe).prompts
    assert "tomato" in prompts
    assert "chicken egg" in prompts
    assert "mixing bowl" in prompts
    assert "pot lid" not in prompts


def test_hsv_signals_separate_red_yellow_and_mixed_states() -> None:
    red = extract_tomato_egg_color_signals(solid_bgr((0, 0, 255)))
    yellow = extract_tomato_egg_color_signals(solid_bgr((0, 255, 255)))
    mixed_frame = solid_bgr((0, 0, 255))
    mixed_frame[:, 50:] = (0, 255, 255)
    mixed = extract_tomato_egg_color_signals(mixed_frame)
    dark = extract_tomato_egg_color_signals(solid_bgr((20, 20, 20)))

    assert red.state == "red_dominant"
    assert yellow.state == "yellow_dominant"
    assert mixed.state == "red_yellow_mixed"
    assert dark.state == "uncertain"


def test_yellow_roi_signal_is_only_partial_evidence_for_cooked_egg() -> None:
    recipe = load_recipe("sop/tomato_egg.json")
    engine = StateEngine(
        session_id="ses_tomato_egg", recipe=recipe, started_at=NOW
    )

    # Complete preparation with independent object + VLM evidence and one repeat.
    engine.consume(
        event(
            1,
            "perception.objects_present",
            {"step_id": "step_01_prepare", "state": "tomato_egg_tools_ready"},
        )
    )
    engine.consume(
        event(
            2,
            "vlm.step_assessment",
            {"step_id": "step_01_prepare", "phase": "likely_complete"},
        )
    )
    advanced = engine.consume(
        event(
            3,
            "vlm.step_assessment",
            {"step_id": "step_01_prepare", "phase": "likely_complete"},
        )
    )
    assert advanced.context.current_step_id == "step_02_scramble_egg"

    signals = extract_tomato_egg_color_signals(solid_bgr((0, 255, 255)))
    color_event = create_color_evidence_event(
        signals,
        session_id="ses_tomato_egg",
        seq=4,
        step_id="step_02_scramble_egg",
        frame_id="frm_4",
        t_device_ms=400,
        t_server_est=NOW + timedelta(milliseconds=400),
        received_at=NOW + timedelta(milliseconds=400),
    )
    result = engine.consume(color_event)
    assert result.status == "evidence_added"
    assert result.context.step_progress.score == 0.3
    assert result.context.current_step_id == "step_02_scramble_egg"
