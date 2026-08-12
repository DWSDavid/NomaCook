"""Tests for frame observation builder, session manifest, capture validator."""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.data.capture import (
    build_frame_observation,
    build_session_manifest,
    validate_capture_session,
    SCHEMA_VERSION,
    MANIFEST_SCHEMA,
)


@dataclass
class _FakeHand:
    handedness: str = "Right"
    landmarks_px: np.ndarray = field(default_factory=lambda: np.random.rand(21, 2) * 100)
    box: tuple = (10, 10, 50, 50)
    grip_closure: float = 0.7
    @property
    def palm_center(self): return (30.0, 30.0)


def test_build_observation_has_21_landmarks():
    hand = _FakeHand()
    obs = build_frame_observation(
        session_id="s1", seq_no=0, frame_idx=0, pts_ms=0,
        frame_width=640, frame_height=480, inference_ran=True,
        hands=[hand],
    )
    assert len(obs["hands"]) == 1
    assert len(obs["hands"][0]["landmarks_norm"]) == 21
    assert obs["schema_version"] == SCHEMA_VERSION


def test_build_observation_multiple_hands():
    hands = [_FakeHand("Left"), _FakeHand("Right")]
    obs = build_frame_observation(
        session_id="s1", seq_no=0, frame_idx=0, pts_ms=0,
        frame_width=640, frame_height=480, inference_ran=True,
        hands=hands,
    )
    assert len(obs["hands"]) == 2


def test_build_observation_no_hands_ok():
    obs = build_frame_observation(
        session_id="s1", seq_no=0, frame_idx=0, pts_ms=0,
        frame_width=640, frame_height=480, inference_ran=False,
    )
    assert obs["hands"] == []
    assert not obs["inference_ran"]


def test_build_observation_nan_hand_filtered():
    hand = _FakeHand()
    hand.landmarks_px[0] = [float("nan"), float("nan")]
    obs = build_frame_observation(
        session_id="s1", seq_no=0, frame_idx=0, pts_ms=0,
        frame_width=640, frame_height=480, inference_ran=True,
        hands=[hand],
    )
    lm0 = obs["hands"][0]["landmarks_norm"][0]
    assert lm0 == [0.0, 0.0]


def test_build_observation_inf_grip_closure_zeroed():
    hand = _FakeHand(grip_closure=float("inf"))
    obs = build_frame_observation(
        session_id="s1", seq_no=0, frame_idx=0, pts_ms=0,
        frame_width=640, frame_height=480, inference_ran=True,
        hands=[hand],
    )
    assert obs["hands"][0]["grip_closure"] == 0.0


def test_machine_labels_never_ground_truth():
    obs = build_frame_observation(
        session_id="s1", seq_no=0, frame_idx=0, pts_ms=0,
        frame_width=640, frame_height=480, inference_ran=True,
        current_step_id="tomato_held",
        emitted_events=[{"type": "HOLDING_STARTED", "confidence": 0.8}],
    )
    ml = obs["machine_labels"]
    assert ml["is_ground_truth"] is False
    assert ml["label_source"] == "state_engine_and_geometry_v1"


def test_build_manifest_correct_schema(tmp_path):
    (tmp_path / "observations.jsonl").touch()
    (tmp_path / "events.jsonl").touch()
    m = build_session_manifest(
        session_id="s1", task_id="t1", source="0", source_type="camera",
        frame_width=640, frame_height=480, fps=30, detect_every=3,
        started_at=1000, ended_at=1010, session_dir=tmp_path,
        raw_video=False, annotated_video=False,
    )
    assert m["schema_version"] == MANIFEST_SCHEMA
    assert m["label_policy"]["is_ground_truth"] is False


def test_manifest_raw_video_present(tmp_path):
    (tmp_path / "observations.jsonl").touch()
    (tmp_path / "raw_video.mp4").touch()
    m = build_session_manifest(
        session_id="s1", task_id="t1", source="0", source_type="camera",
        frame_width=640, frame_height=480, fps=30, detect_every=3,
        started_at=1000, ended_at=1010, session_dir=tmp_path,
        raw_video=True, annotated_video=False,
    )
    assert m["artifacts"]["raw_video"] is not None


def test_manifest_raw_video_absent(tmp_path):
    (tmp_path / "observations.jsonl").touch()
    m = build_session_manifest(
        session_id="s1", task_id="t1", source="0", source_type="camera",
        frame_width=640, frame_height=480, fps=30, detect_every=3,
        started_at=1000, ended_at=1010, session_dir=tmp_path,
        raw_video=False, annotated_video=False,
    )
    assert m["artifacts"]["raw_video"] is None


def test_validator_passes_minimal_session(tmp_path):
    # write valid observations
    obs_lines = []
    for i in range(5):
        hand = _FakeHand()
        obs = build_frame_observation(
            session_id="s1", seq_no=i, frame_idx=i, pts_ms=i * 33.3,
            frame_width=640, frame_height=480, inference_ran=i % 3 == 0,
            hands=[hand],
            current_step_id="ready",
            emitted_events=[],
        )
        obs_lines.append(json.dumps(obs, ensure_ascii=False))
    (tmp_path / "observations.jsonl").write_text("\n".join(obs_lines))
    (tmp_path / "events.jsonl").touch()

    m = build_session_manifest(
        session_id="s1", task_id="t1", source="test", source_type="video",
        frame_width=640, frame_height=480, fps=30, detect_every=3,
        started_at=1000, ended_at=1010, session_dir=tmp_path,
    )
    (tmp_path / "session_manifest.json").write_text(json.dumps(m))

    passed, result = validate_capture_session(tmp_path)
    assert passed
    assert result["frames"] == 5
    assert result["frames_with_hands"] == 5
    assert result["missing_or_invalid"] == 0


def test_validator_detects_corrupt_jsonl(tmp_path):
    (tmp_path / "observations.jsonl").write_text('{"schema_version": "wrong"}\nnot json\n')
    (tmp_path / "events.jsonl").touch()
    m = build_session_manifest(
        session_id="s1", task_id="t1", source="t", source_type="video",
        frame_width=640, frame_height=480, fps=30, detect_every=3,
        started_at=1000, ended_at=1010, session_dir=tmp_path,
    )
    (tmp_path / "session_manifest.json").write_text(json.dumps(m))
    passed, result = validate_capture_session(tmp_path)
    assert not passed
    assert result["missing_or_invalid"] > 0


def test_validator_detects_non_monotonic(tmp_path):
    obs_lines = [
        json.dumps(build_frame_observation(
            session_id="s1", seq_no=0, frame_idx=5, pts_ms=100,
            frame_width=640, frame_height=480, inference_ran=False,
        )),
        json.dumps(build_frame_observation(
            session_id="s1", seq_no=1, frame_idx=3, pts_ms=200,
            frame_width=640, frame_height=480, inference_ran=False,
        )),
    ]
    (tmp_path / "observations.jsonl").write_text("\n".join(obs_lines))
    (tmp_path / "events.jsonl").touch()
    m = build_session_manifest(
        session_id="s1", task_id="t1", source="t", source_type="video",
        frame_width=640, frame_height=480, fps=30, detect_every=3,
        started_at=1000, ended_at=1010, session_dir=tmp_path,
    )
    (tmp_path / "session_manifest.json").write_text(json.dumps(m))
    _, result = validate_capture_session(tmp_path)
    assert result["missing_or_invalid"] > 0


def test_secrets_check_rejects_key():
    from server.data.capture import secrets_check
    with pytest.raises(ValueError):
        secrets_check('{"key": "sk-ws-12345"}')


def test_hand_with_lt_21_landmarks_filtered():
    hand = _FakeHand()
    hand.landmarks_px = np.random.rand(10, 2) * 100  # only 10
    obs = build_frame_observation(
        session_id="s1", seq_no=0, frame_idx=0, pts_ms=0,
        frame_width=640, frame_height=480, inference_ran=True,
        hands=[hand],
    )
    assert obs["hands"] == []
