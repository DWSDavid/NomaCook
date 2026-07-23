from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from server.engine import load_recipe
from server.pipeline.narrate import (
    complete_item,
    intro_item,
    question_item,
    schedule,
    synthesize_say,
    transition_item,
)

RECIPE = load_recipe("sop/tomato_egg.json")


def test_items_carry_pts_kind_and_chinese_text():
    intro = intro_item(RECIPE)
    assert intro["pts_ms"] == 0.0 and intro["kind"] == "intro"
    assert "番茄炒鸡蛋" in intro["text"] and RECIPE.steps[0].instruction in intro["text"]

    tr = transition_item(RECIPE, "step_01_prepare", "step_02_scramble_egg", 600.0)
    assert tr["pts_ms"] == 600.0 and tr["kind"] == "step"
    assert RECIPE.steps[1].instruction in tr["text"]

    q = question_item("番茄切好了吗？", 900.0)
    assert q["kind"] == "question" and q["text"] == "番茄切好了吗？"

    done = complete_item(2400.0)
    assert done["kind"] == "complete" and done["pts_ms"] == 2400.0


def test_schedule_shifts_overlapping_items_but_keeps_early_starts():
    items = [{"pts_ms": 0.0}, {"pts_ms": 1000.0}, {"pts_ms": 9000.0}]
    starts = schedule(items, durations_ms=[3000.0, 2000.0, 1000.0], gap_ms=300.0)
    assert starts == [0.0, 3300.0, 9000.0]


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("say") is None,
    reason="macOS say unavailable")
def test_say_synthesizes_playable_clip(tmp_path: Path):
    out = tmp_path / "clip.aiff"
    synthesize_say("你好", out)
    assert out.stat().st_size > 1000
