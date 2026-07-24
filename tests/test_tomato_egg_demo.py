from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

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


def test_tomato_egg_is_the_exact_seven_step_demo() -> None:
    recipe = load_recipe("sop/tomato_egg.json")
    assert recipe.dish == "番茄炒蛋"
    assert [step.id for step in recipe.steps] == [
        "step_01_prepare",
        "step_02_beat_eggs",
        "step_03_cut_tomatoes",
        "step_04_scramble_eggs",
        "step_05_fry_tomatoes",
        "step_06_combine",
        "step_07_plate",
    ]
    assert [step.title for step in recipe.steps] == [
        "准备食材和工具",
        "打散鸡蛋",
        "切好两个番茄",
        "炒鸡蛋",
        "炒番茄",
        "鸡蛋和番茄混合",
        "关火装盘",
    ]
    assert recipe.steps[0].instruction.startswith("请准备两个番茄、两个鸡蛋")
    assert recipe.steps[1].instruction == "请把两个鸡蛋打入碗中，再用筷子搅拌均匀。"
    assert recipe.steps[2].instruction == "请依次把两个番茄全部切成大小接近的块。"
    assert recipe.steps[-1].completion_message == "番茄炒蛋制作完成。本次操作已结束。"

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


def test_in_progress_cannot_finish_a_step_and_yellow_is_only_partial_evidence() -> None:
    recipe = load_recipe("sop/tomato_egg.json")
    engine = StateEngine(
        session_id="ses_tomato_egg", recipe=recipe, started_at=NOW
    )

    # Repeated in-progress signals plus object presence must not recreate the
    # old false advance: only a visually complete result may cross threshold.
    engine.consume(
        event(
            1,
            "perception.objects_present",
            {"step_id": "step_01_prepare", "state": "core_ingredients_ready"},
        )
    )
    for seq in (2, 3):
        result = engine.consume(
            event(
                seq,
                "vlm.step_assessment",
                {"step_id": "step_01_prepare", "phase": "in_progress"},
            )
        )
        assert result.context.current_step_id == "step_01_prepare"

    engine.consume(event(4, "vlm.step_assessment", {
        "step_id": "step_01_prepare", "phase": "likely_complete"}))
    advanced = engine.context
    assert advanced.current_step_id == "step_02_beat_eggs"

    # Advance the result-only prep stages to the real cooking step.
    for seq, step_id in (
        (5, "step_02_beat_eggs"),
        (6, "step_03_cut_tomatoes"),
    ):
        engine.consume(event(seq, "vlm.step_assessment", {
            "step_id": step_id, "phase": "likely_complete"}))
    assert engine.context.current_step_id == "step_04_scramble_eggs"

    signals = extract_tomato_egg_color_signals(solid_bgr((0, 255, 255)))
    color_event = create_color_evidence_event(
        signals,
        session_id="ses_tomato_egg",
        seq=7,
        step_id="step_04_scramble_eggs",
        frame_id="frm_7",
        t_device_ms=700,
        t_server_est=NOW + timedelta(milliseconds=700),
        received_at=NOW + timedelta(milliseconds=700),
    )
    result = engine.consume(color_event)
    assert result.status == "evidence_added"
    step4 = next(s for s in engine.recipe.steps if s.id == "step_04_scramble_eggs")
    yellow_rule = next(
        r for r in step4.completion_policy.evidence_rules if r.id == "egg_yellow_roi"
    )
    assert result.context.step_progress.score == pytest.approx(yellow_rule.weight)
    # The point under test: yellow ROI alone must stay below the threshold.
    assert yellow_rule.weight < step4.completion_policy.threshold
    assert result.context.current_step_id == "step_04_scramble_eggs"

    first_visual = engine.consume(event(8, "vlm.step_assessment", {
        "step_id": "step_04_scramble_eggs", "phase": "likely_complete"}))
    assert first_visual.context.step_progress.consecutive_hits == 1

    weak_color_again = create_color_evidence_event(
        signals,
        session_id="ses_tomato_egg",
        seq=9,
        step_id="step_04_scramble_eggs",
        frame_id="frm_9",
        t_device_ms=900,
        t_server_est=NOW + timedelta(milliseconds=900),
        received_at=NOW + timedelta(milliseconds=900),
    )
    still_step4 = engine.consume(weak_color_again)
    assert still_step4.context.step_progress.consecutive_hits == 1
    assert still_step4.context.current_step_id == "step_04_scramble_eggs"

    second_visual = engine.consume(event(10, "vlm.step_assessment", {
        "step_id": "step_04_scramble_eggs", "phase": "likely_complete"}))
    assert second_visual.context.current_step_id == "step_05_fry_tomatoes"
