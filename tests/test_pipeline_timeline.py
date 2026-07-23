from __future__ import annotations

import json
from pathlib import Path

from server.pipeline.timeline import (
    KeyframeSampler,
    StateSnapshot,
    append_jsonl,
    diff_snapshots,
    keyframe_row,
)


def _snap(pts: float, **overrides) -> StateSnapshot:
    base = dict(
        pts_ms=pts, frame_idx=int(pts // 33), step_id="step_01_prepare",
        context_version=1, score=0.0, pending_question=None,
        detections=(("bowl", 0.7), ("egg", 0.5)), color_state=None,
    )
    base.update(overrides)
    return StateSnapshot(**base)


def test_sampler_fires_on_first_frame_then_every_interval():
    sampler = KeyframeSampler(interval_ms=3000.0)
    fired = [pts for pts in (0.0, 1000.0, 2999.0, 3000.0, 5900.0, 6000.0, 9100.0)
             if sampler.due(pts)]
    assert fired == [0.0, 3000.0, 6000.0, 9100.0]


def test_diff_reports_step_score_objects_and_color_changes():
    prev = _snap(0.0, score=0.3, color_state="uncertain")
    cur = _snap(
        3000.0, step_id="step_02_scramble_egg", context_version=5, score=0.0,
        detections=(("wok", 0.8), ("egg", 0.5)), color_state="yellow_dominant",
    )
    diff = diff_snapshots(prev, cur)
    assert diff["step_changed"] is True
    assert diff["score_delta"] == -0.3
    assert diff["objects_appeared"] == ["wok"]
    assert diff["objects_gone"] == ["bowl"]
    assert diff["color_changed"] is True


def test_first_keyframe_diff_is_empty_baseline():
    diff = diff_snapshots(None, _snap(0.0))
    assert diff == {"baseline": True}


def test_keyframe_row_roundtrips_through_jsonl(tmp_path: Path):
    row = keyframe_row(_snap(3000.0, score=0.4), {"baseline": True}, "kf_000090_3000ms.jpg")
    path = tmp_path / "timeline.jsonl"
    append_jsonl(path, row)
    append_jsonl(path, row)
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0]["jpg"] == "kf_000090_3000ms.jpg"
    assert lines[0]["step_id"] == "step_01_prepare"
    assert lines[0]["score"] == 0.4
    assert lines[0]["diff"] == {"baseline": True}
