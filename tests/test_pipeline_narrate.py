from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from server.engine import load_recipe
from server.pipeline.narrate import (
    complete_item,
    fit_schedule,
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
    assert "番茄炒蛋" in intro["text"] and RECIPE.steps[0].instruction in intro["text"]
    assert "第一步，准备食材和工具" in intro["text"]

    tr = transition_item(RECIPE, "step_01_prepare", "step_02_beat_eggs", 600.0)
    assert tr["pts_ms"] == 600.0 and tr["kind"] == "step"
    assert RECIPE.steps[1].instruction in tr["text"]
    assert RECIPE.steps[0].completion_message in tr["text"]

    short = transition_item(
        RECIPE, "step_04_scramble_eggs", "step_05_fry_tomatoes", 111_000.0,
        include_instruction=False,
    )
    assert short["text"] == RECIPE.steps[3].completion_message

    q = question_item("番茄切好了吗？", 900.0)
    assert q["kind"] == "question" and q["text"] == "番茄切好了吗？"

    done = complete_item(2400.0, RECIPE)
    assert done["kind"] == "complete" and done["pts_ms"] == 2400.0
    assert done["text"] == "番茄炒蛋做好了。妈，我会做饭了。"


def test_schedule_shifts_overlapping_items_but_keeps_early_starts():
    items = [{"pts_ms": 0.0}, {"pts_ms": 1000.0}, {"pts_ms": 9000.0}]
    starts = schedule(items, durations_ms=[3000.0, 2000.0, 1000.0], gap_ms=300.0)
    assert starts == [0.0, 3300.0, 9000.0]


def test_fit_schedule_drops_optional_cues_that_would_delay_a_step():
    items = [
        {"pts_ms": 0.0, "kind": "intro"},
        {"pts_ms": 1000.0, "kind": "preview"},
        {"pts_ms": 2000.0, "kind": "remark"},
        {"pts_ms": 6000.0, "kind": "step"},
        {"pts_ms": 12_000.0, "kind": "remark"},
    ]
    selected, starts = fit_schedule(
        items, [5000.0, 4000.0, 2000.0, 3000.0, 2000.0],
        end_ms=20_000.0,
    )
    assert selected == [0, 3, 4]
    assert starts == [0.0, 6000.0, 12_000.0]


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("say") is None,
    reason="macOS say unavailable")
def test_say_synthesizes_playable_clip(tmp_path: Path):
    out = tmp_path / "clip.aiff"
    synthesize_say("你好", out)
    assert out.stat().st_size > 1000


def test_preview_item_pre_announces_next_step():
    from server.pipeline.narrate import preview_item

    item = preview_item(RECIPE, "step_01_prepare", 5000.0)
    assert item is not None
    assert item["kind"] == "preview" and item["pts_ms"] == 5000.0
    assert "第二步，打散鸡蛋" in item["text"]
    assert "等我确认后再开始" in item["text"]


def test_preview_item_returns_none_on_last_step():
    from server.pipeline.narrate import preview_item

    assert preview_item(RECIPE, RECIPE.steps[-1].id, 5000.0) is None


def test_remark_item_carries_vlm_one_liner():
    from server.pipeline.narrate import remark_item

    item = remark_item("注意，锅边的油在冒烟", 7000.0)
    assert item["kind"] == "remark" and item["pts_ms"] == 7000.0
    assert item["text"] == "注意，锅边的油在冒烟"


def test_probe_seg_color_thresholds():
    # Pure-function color gate for the FastSAM probe (no model load needed).
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "harness"))
    from probe_seg import classify_mask_color

    assert classify_mask_color((5, 180, 150)) == "tomato_red"
    assert classify_mask_color((175, 160, 140)) == "tomato_red"  # hue wrap
    assert classify_mask_color((28, 150, 180)) == "egg_yellow"
    assert classify_mask_color((28, 40, 180)) is None  # washed out
    assert classify_mask_color((100, 200, 150)) is None  # blue-ish


def test_narrate_cache_includes_backend_voice(tmp_path: Path, monkeypatch):
    from server.pipeline import narrate

    run_root = tmp_path / "run"
    run_root.mkdir()
    (run_root / "annotated.mp4").write_bytes(b"video")
    (run_root / "narration.json").write_text(
        json.dumps([{"pts_ms": 0.0, "kind": "intro", "text": "你好"}]),
        encoding="utf-8",
    )
    calls: list[tuple[str, str]] = []

    def fake_say(text: str, out_path: Path, voice: str) -> None:
        calls.append((text, voice))
        out_path.write_bytes(b"audio")

    monkeypatch.setattr(narrate, "_require_ffmpeg", lambda: None)
    monkeypatch.setattr(narrate, "_duration_ms", lambda path: 1_000.0)
    monkeypatch.setattr(narrate, "synthesize_say", fake_say)
    monkeypatch.setattr(
        narrate.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args=[], returncode=0),
    )

    narrate.narrate_run(run_root, "say", "Tingting")
    narrate.narrate_run(run_root, "say", "Tingting")
    narrate.narrate_run(run_root, "say", "Meijia")

    assert calls == [("你好", "Tingting"), ("你好", "Meijia")]
    meta = json.loads(
        (run_root / "narration_clips/clip_000.meta.json").read_text()
    )
    assert meta["voice"] == "Meijia" and meta["spoken_text"] == "你好"
