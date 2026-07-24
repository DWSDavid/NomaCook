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
        "final_step_id": "step_07_plate", "final_status": "completed",
        "transitions": [
            {"decision_id": "d1", "completed_step_id": "step_01_prepare",
             "next_step_id": "step_02_beat_eggs", "score": 0.8, "pts_ms": 600.0},
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
    assert "step_02_beat_eggs" in text
    assert "kf_000000_0ms.jpg" in text
    assert "completed" in text


def test_report_links_the_exact_frame_assessed_by_gemini(tmp_path: Path):
    paths = _seed(tmp_path / "run_vlm")
    frame_id = "frame_000150"
    (paths.keyframes_dir / f"vlm_{frame_id}.jpg").write_bytes(b"jpeg")
    paths.events.write_text(
        json.dumps(
            {
                "type": "vlm.step_assessment",
                "t_device_ms": 5000.0,
                "confidence": 0.9,
                "payload": {
                    "step_id": "step_01_prepare",
                    "phase": "in_progress",
                    "risk_level": "none",
                    "reason": "stub",
                    "frame_id": frame_id,
                },
            }
        )
        + "\n"
    )

    text = write_report(paths).read_text(encoding="utf-8")

    assert f"keyframes/vlm_{frame_id}.jpg" in text
