from __future__ import annotations

import numpy as np

from harness import eval_tomato_to_fridge as evaluator


def test_presentation_contains_bilingual_recognized_and_next_copy() -> None:
    presentation = evaluator._presentation_for(
        "tomato_held", "tomato_in_transit"
    )

    assert presentation["recognized_zh"] == "已拿起番茄"
    assert presentation["recognized_en"] == "Tomato picked up"
    assert presentation["next_zh"] == "请把番茄移向冰箱。"
    assert presentation["next_en"] == "Move the tomato toward the refrigerator."


def test_local_narration_keeps_only_meaningful_phase_changes() -> None:
    transitions = [
        {"step_id": step_id, "pts_ms": index * 1000.0}
        for index, step_id in enumerate([
            "ready",
            "tomato_on_table",
            "hand_near_tomato",
            "tomato_held",
            "tomato_in_transit",
            "fridge_interaction",
            "candidate_inside_fridge",
            "tomato_released_inside",
        ])
    ]

    items = evaluator._local_narration_items(transitions)

    assert [item["step_id"] for item in items] == [
        "tomato_on_table",
        "tomato_held",
        "fridge_interaction",
        "candidate_inside_fridge",
        "tomato_released_inside",
    ]
    assert items[-1]["text"] == "番茄已经稳定放入冰箱，任务完成。"


def test_status_panel_draws_large_dark_header_and_debug_footer() -> None:
    frame = np.full((1080, 1920, 3), 255, dtype=np.uint8)
    presentation = evaluator._presentation_for(
        "tomato_held", "tomato_in_transit"
    )

    evaluator._draw_status_panel(
        frame,
        presentation,
        "YOLO 21ms | score 0.5/0.8 | evidence 2/2",
    )

    assert frame[:170].mean() < 90
    assert frame[-36:].mean() < 90
    assert frame[210:900].mean() == 255


def test_parser_defaults_to_local_meijia_voice() -> None:
    args = evaluator.build_parser().parse_args([
        "--source", "input.mov",
        "--run-dir", "output",
    ])

    assert args.local_narration is False
    assert args.voice == "Meijia"
