from __future__ import annotations

import json
from pathlib import Path

from server.pipeline.report import write_report
from server.pipeline.session import SessionPaths


def _seed(root: Path) -> SessionPaths:
    paths = SessionPaths(root=root)
    root.mkdir(parents=True)
    (root / "keyframes").mkdir()
    paths.meta.write_text(json.dumps({
        "session_id": "ses_x", "video": "v.mp4", "sop": "sop/tomato_egg.json",
        "fps": 30.0, "frames": 90, "events": 20, "vlm_mode": "off",
        "final_step_id": "step_04_combine_and_plate", "final_status": "completed",
        "transitions": [
            {"decision_id": "d1", "completed_step_id": "step_01_prepare",
             "next_step_id": "step_02_scramble_egg", "score": 0.7, "pts_ms": 600.0},
        ],
    }, ensure_ascii=False))
    paths.timeline.write_text(json.dumps({
        "pts_ms": 0.0, "frame_idx": 0, "step_id": "step_01_prepare",
        "context_version": 1, "score": 0.0, "pending_question": None,
        "detections": [["bowl", 0.7]], "color_state": None,
        "jpg": "kf_000000_0ms.jpg", "diff": {"baseline": True},
    }, ensure_ascii=False) + "\n")
    paths.events.write_text("")
    return paths


def test_report_contains_transitions_and_keyframe_table(tmp_path: Path):
    paths = _seed(tmp_path / "run_a")
    out = write_report(paths)
    text = out.read_text(encoding="utf-8")
    assert out == paths.report
    assert "step_01_prepare" in text
    assert "step_02_scramble_egg" in text
    assert "kf_000000_0ms.jpg" in text
    assert "completed" in text
