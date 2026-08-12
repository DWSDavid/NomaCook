"""Tests for review queue builder, gold labels, validator, and metrics."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.data.review import (
    build_gold_label_template,
    apply_review,
    _stable_id,
    build_review_queue,
    validate_review_labels,
    build_label_metrics,
    GOLD_SCHEMA,
    REVIEW_SCHEMA,
    CLIP_PADDING_MS,
)


def _write_mini_session(tmp_path, frames=50, fps=30.0, completed=True):
    """Write a minimal observations.jsonl + events.jsonl + summary + manifest."""
    obs = []
    for i in range(frames):
        obs.append({
            "schema_version": "noma.frame_observation.v1",
            "session_id": "ses_test", "frame_idx": i,
            "pts_ms": round(i * 1000 / fps, 2),
            "frame_width": 640, "frame_height": 480,
            "inference_ran": i % 3 == 0,
            "detections": [],
            "hands": [],
            "machine_labels": {
                "current_step_id": "ready", "step_status": "in_progress",
                "context_version": i, "emitted_events": [],
                "label_source": "test", "is_ground_truth": False,
            },
        })
    (tmp_path / "observations.jsonl").write_text(
        "\n".join(json.dumps(o) for o in obs))

    evts = [{"type": "OBJECT_PRESENT", "t_device_ms": i * 1000 / fps,
             "confidence": 0.9, "payload": {}} for i in range(frames)]
    (tmp_path / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in evts))

    summary = {
        "session_id": "ses_test", "step_status": "completed" if completed else "in_progress",
        "final_step_id": "tomato_held", "predicted_transitions": [
            {"step_id": "ready", "first_frame": 0, "pts_ms": 0},
            {"step_id": "tomato_held", "first_frame": 30, "pts_ms": 1000},
        ],
    }
    (tmp_path / "summary.json").write_text(json.dumps(summary))
    manifest = {
        "schema_version": "noma.capture_session.v1",
        "session_id": "ses_test", "task_id": "test_task",
        "frame_width": 640, "frame_height": 480, "fps": fps,
        "detect_every": 3, "artifacts": {},
        "label_policy": {"is_ground_truth": False},
    }
    (tmp_path / "session_manifest.json").write_text(json.dumps(manifest))


def _make_fake_mp4(path: Path, w=640, h=480, count=50, fps=30.0):
    import cv2, numpy as np
    out = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    for _ in range(count):
        out.write(np.zeros((h, w, 3), dtype=np.uint8))
    out.release()


# ── Stable IDs ──

def test_stable_id_deterministic():
    a = _stable_id("ses1", "state_transition", "10")
    b = _stable_id("ses1", "state_transition", "10")
    assert a == b


def test_stable_id_different():
    a = _stable_id("ses1", "state_transition", "10")
    b = _stable_id("ses1", "state_transition", "11")
    assert a != b


# ── Gold label template ──

def test_template_not_ground_truth():
    item = {"review_item_id": "rv_123", "reason": "test"}
    gl = build_gold_label_template(item)
    assert gl["is_ground_truth"] is False
    assert gl["reviewer_label"] is None
    assert gl["schema_version"] == GOLD_SCHEMA


def test_apply_review_correct_becomes_gold():
    gl = build_gold_label_template({"review_item_id": "rv_1"})
    gl = apply_review(gl, reviewer_label="correct", event_type="OBJECT_PICKED_UP")
    assert gl["is_ground_truth"] is True
    assert gl["reviewer_label"] == "correct"
    assert gl["reviewed_at"] is not None


def test_apply_review_uncertain_not_gold():
    gl = build_gold_label_template({"review_item_id": "rv_1"})
    gl = apply_review(gl, reviewer_label="uncertain")
    assert gl["is_ground_truth"] is False


def test_apply_review_incorrect_with_event_type_gold():
    gl = build_gold_label_template({"review_item_id": "rv_1"})
    gl = apply_review(gl, reviewer_label="incorrect", event_type="WRONG_EVENT")
    assert gl["is_ground_truth"] is True


# ── Review queue builder ──

def test_build_queue_transitions_present(tmp_path):
    _write_mini_session(tmp_path, completed=True)
    _make_fake_mp4(tmp_path / "raw_video.mp4", count=50)
    items, meta = build_review_queue(tmp_path)
    transitions = [i for i in items if i["reason"] == "state_transition"]
    assert len(transitions) >= 1
    assert transitions[0]["review_item_id"].startswith("rv_")


def test_build_queue_completion_when_completed(tmp_path):
    _write_mini_session(tmp_path, completed=True)
    _make_fake_mp4(tmp_path / "raw_video.mp4", count=50)
    items, _ = build_review_queue(tmp_path)
    completions = [i for i in items if i["reason"] == "task_completion"]
    assert len(completions) == 1


def test_build_queue_session_ending_when_incomplete(tmp_path):
    _write_mini_session(tmp_path, completed=False)
    _make_fake_mp4(tmp_path / "raw_video.mp4", count=50)
    items, _ = build_review_queue(tmp_path)
    se = [i for i in items if i["reason"] == "session_ending_incomplete"]
    assert len(se) == 1


def test_build_queue_no_duplicate_ids(tmp_path):
    _write_mini_session(tmp_path, completed=True)
    _make_fake_mp4(tmp_path / "raw_video.mp4", count=50)
    items, _ = build_review_queue(tmp_path)
    ids = [i["review_item_id"] for i in items]
    assert len(ids) == len(set(ids))


def test_clip_range_not_beyond_boundary(tmp_path):
    _write_mini_session(tmp_path, frames=100, completed=True)
    _make_fake_mp4(tmp_path / "raw_video.mp4", count=100)
    items, _ = build_review_queue(tmp_path, fps=30)
    for item in items:
        assert item["start_frame"] >= 0
        assert item["end_frame"] < 100
        assert item["start_frame"] <= item["end_frame"]


# ── Validator ──

def test_validator_passes_clean_queue(tmp_path):
    _write_mini_session(tmp_path, completed=True)
    _make_fake_mp4(tmp_path / "raw_video.mp4", count=50)
    items, _ = build_review_queue(tmp_path)
    review_dir = tmp_path / "review"
    review_dir.mkdir()
    (review_dir / "review_queue.jsonl").write_text(
        "\n".join(json.dumps(i) for i in items))
    # create clips
    clips_dir = review_dir / "clips"
    clips_dir.mkdir()
    for item in items:
        _make_fake_mp4(clips_dir / item["clip_path"].split("/")[-1],
                       count=item["end_frame"] - item["start_frame"] + 1)
    # create gold label templates
    labels = [build_gold_label_template(i) for i in items]
    (review_dir / "gold_labels.jsonl").write_text(
        "\n".join(json.dumps(g) for g in labels))

    passed, result = validate_review_labels(tmp_path, review_dir)
    assert passed
    assert result["queue_items"] == len(items)
    assert result["gold_labels"] == 0


def test_validator_fails_orphan_label(tmp_path):
    _write_mini_session(tmp_path, completed=True)
    _make_fake_mp4(tmp_path / "raw_video.mp4", count=50)
    items, _ = build_review_queue(tmp_path)
    review_dir = tmp_path / "review"
    review_dir.mkdir()
    (review_dir / "review_queue.jsonl").write_text(
        "\n".join(json.dumps(i) for i in items))
    clips_dir = review_dir / "clips"
    clips_dir.mkdir()
    for item in items:
        _make_fake_mp4(clips_dir / item["clip_path"].split("/")[-1],
                       count=item["end_frame"] - item["start_frame"] + 1)
    # orphan label
    orphan = build_gold_label_template({"review_item_id": "rv_nonexistent"})
    orphan["reviewer_label"] = "correct"; orphan["event_type"] = "EV"; orphan["is_ground_truth"] = True
    (review_dir / "gold_labels.jsonl").write_text(json.dumps(orphan))

    passed, result = validate_review_labels(tmp_path, review_dir)
    assert not passed


def test_validator_detects_uncertain_as_not_gold(tmp_path):
    _write_mini_session(tmp_path, completed=True)
    _make_fake_mp4(tmp_path / "raw_video.mp4", count=50)
    items, _ = build_review_queue(tmp_path)
    review_dir = tmp_path / "review"
    review_dir.mkdir()
    (review_dir / "review_queue.jsonl").write_text(
        "\n".join(json.dumps(i) for i in items))
    clips_dir = review_dir / "clips"
    clips_dir.mkdir()
    for item in items:
        _make_fake_mp4(clips_dir / item["clip_path"].split("/")[-1],
                       count=item["end_frame"] - item["start_frame"] + 1)
    gl = apply_review(build_gold_label_template(items[0]),
                      reviewer_label="uncertain")
    if items: gl["is_ground_truth"] = True  # incorrectly set
    (review_dir / "gold_labels.jsonl").write_text(json.dumps(gl))

    passed, result = validate_review_labels(tmp_path, review_dir)
    assert not passed


# ── Metrics ──

def test_metrics_no_golds_show_null_accuracy(tmp_path):
    _write_mini_session(tmp_path, completed=True)
    _make_fake_mp4(tmp_path / "raw_video.mp4", count=50)
    items, _ = build_review_queue(tmp_path)
    review_dir = tmp_path / "review"
    review_dir.mkdir()
    (review_dir / "review_queue.jsonl").write_text("\n".join(json.dumps(i) for i in items))
    clips_dir = review_dir / "clips"
    clips_dir.mkdir()
    for item in items:
        _make_fake_mp4(clips_dir / item["clip_path"].split("/")[-1],
                       count=item["end_frame"] - item["start_frame"] + 1)
    (review_dir / "gold_labels.jsonl").write_text("")

    m = build_label_metrics(tmp_path, review_dir)
    assert m["gold_count"] == 0
    assert m["accuracy_on_reviewed"] is None
