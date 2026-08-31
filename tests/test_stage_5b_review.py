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


def test_apply_review_keeps_boundary_frame_zero():
    gl = build_gold_label_template({"review_item_id": "rv_1"})
    gl = apply_review(
        gl,
        reviewer_label="correct",
        event_type="OBJECT_PRESENT",
        boundary_frame=0,
    )
    assert gl["boundary_frame"] == 0


# ── Review queue builder ──

def test_build_queue_transitions_present(tmp_path):
    _write_mini_session(tmp_path, completed=True)
    _make_fake_mp4(tmp_path / "raw_video.mp4", count=50)
    items, meta = build_review_queue(tmp_path)
    transitions = [i for i in items if i["reason"] == "state_transition"]
    assert len(transitions) >= 1
    assert transitions[0]["review_item_id"].startswith("rv_")


def test_distinct_transitions_inside_merge_window_are_preserved(tmp_path):
    _write_mini_session(tmp_path, completed=True)

    items, _ = build_review_queue(tmp_path)

    transitions = [item for item in items if item["reason"] == "state_transition"]
    assert [
        item["machine_label"]["predicted_step_id"] for item in transitions
    ] == ["ready", "tomato_held"]


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


def test_zero_confidence_event_is_queued(tmp_path):
    _write_mini_session(tmp_path, completed=True)
    events_path = tmp_path / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    events.append({
        "type": "OBJECT_PRESENT",
        "t_device_ms": 500,
        "confidence": 0.0,
        "payload": {},
    })
    events_path.write_text("\n".join(json.dumps(event) for event in events))

    items, _ = build_review_queue(tmp_path)

    assert any(
        item["reason"] == "low_confidence"
        and item["machine_label"]["confidence"] == 0.0
        for item in items
    )


def test_opposite_events_in_same_frame_are_queued_as_conflict(tmp_path):
    _write_mini_session(tmp_path, completed=True)
    events = [
        {"type": "HAND_NEAR_STARTED", "t_device_ms": 500, "confidence": 0.9, "payload": {}},
        {"type": "HAND_NEAR_ENDED", "t_device_ms": 500, "confidence": 0.9, "payload": {}},
    ]
    (tmp_path / "events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events)
    )

    items, _ = build_review_queue(tmp_path)

    conflicts = [item for item in items if item["reason"] == "conflict"]
    assert len(conflicts) == 1
    assert conflicts[0]["machine_label"]["event_type"] == "HAND_NEAR_ENDED|HAND_NEAR_STARTED"


def test_review_item_id_does_not_change_when_earlier_item_is_added(tmp_path):
    _write_mini_session(tmp_path, completed=True)
    original, _ = build_review_queue(tmp_path)
    original_id = next(
        item["review_item_id"]
        for item in original
        if item["reason"] == "state_transition"
        and item["machine_label"]["predicted_step_id"] == "tomato_held"
    )
    events_path = tmp_path / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    events.append({
        "type": "OBJECT_PRESENT",
        "t_device_ms": 500,
        "confidence": 0.2,
        "payload": {},
    })
    events_path.write_text("\n".join(json.dumps(event) for event in events))

    updated, _ = build_review_queue(tmp_path)
    updated_id = next(
        item["review_item_id"]
        for item in updated
        if item["reason"] == "state_transition"
        and item["machine_label"]["predicted_step_id"] == "tomato_held"
    )

    assert updated_id == original_id


def test_capture_session_ids_are_unique_unless_explicitly_set():
    from harness.live_tomato_to_fridge import build_parser, resolve_session_id

    parser = build_parser()
    generated_a = resolve_session_id(parser.parse_args([]))
    generated_b = resolve_session_id(parser.parse_args([]))
    explicit = resolve_session_id(parser.parse_args(["--session-id", "ses_pilot_1"]))

    assert generated_a.startswith("ses_tomato_fridge_")
    assert generated_a != generated_b
    assert explicit == "ses_pilot_1"


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


def test_validator_rejects_duplicate_gold_label_ids(tmp_path):
    _write_mini_session(tmp_path, completed=True)
    items, _ = build_review_queue(tmp_path)
    review_dir = tmp_path / "review"
    review_dir.mkdir()
    (review_dir / "review_queue.jsonl").write_text(
        "\n".join(json.dumps(item) for item in items)
    )
    clips_dir = review_dir / "clips"
    clips_dir.mkdir()
    for item in items:
        (clips_dir / Path(item["clip_path"]).name).touch()
    duplicate = build_gold_label_template(items[0])
    (review_dir / "gold_labels.jsonl").write_text(
        "\n".join((json.dumps(duplicate), json.dumps(duplicate)))
    )

    passed, result = validate_review_labels(tmp_path, review_dir)

    assert not passed
    assert any("duplicate gold label" in error for error in result["errors"])


@pytest.mark.parametrize(
    ("field", "value", "expected_error"),
    [
        ("schema_version", "wrong.schema", "bad gold schema"),
        ("reviewer_label", "maybe", "invalid reviewer_label"),
    ],
)
def test_validator_rejects_malformed_gold_labels(
    tmp_path, field, value, expected_error
):
    _write_mini_session(tmp_path, completed=True)
    items, _ = build_review_queue(tmp_path)
    review_dir = tmp_path / "review"
    review_dir.mkdir()
    (review_dir / "review_queue.jsonl").write_text(
        "\n".join(json.dumps(item) for item in items)
    )
    clips_dir = review_dir / "clips"
    clips_dir.mkdir()
    for item in items:
        (clips_dir / Path(item["clip_path"]).name).touch()
    label = build_gold_label_template(items[0])
    label[field] = value
    (review_dir / "gold_labels.jsonl").write_text(json.dumps(label))

    passed, result = validate_review_labels(tmp_path, review_dir)

    assert not passed
    assert any(expected_error in error for error in result["errors"])


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


def test_queue_rebuild_preserves_reviewed_labels(tmp_path, monkeypatch):
    from harness.build_review_queue import main as build_main

    _write_mini_session(tmp_path, completed=True)
    _make_fake_mp4(tmp_path / "raw_video.mp4", count=50)
    monkeypatch.setattr(sys, "argv", ["build_review_queue", str(tmp_path)])
    with pytest.raises(SystemExit) as first_exit:
        build_main()
    assert first_exit.value.code == 0

    labels_path = tmp_path / "review" / "gold_labels.jsonl"
    labels = [json.loads(line) for line in labels_path.read_text().splitlines()]
    reviewed = apply_review(
        labels[0], reviewer_label="correct", event_type="STATE_TRANSITION"
    )
    labels[0] = reviewed
    labels_path.write_text("\n".join(json.dumps(label) for label in labels))

    with pytest.raises(SystemExit) as second_exit:
        build_main()
    assert second_exit.value.code == 0

    rebuilt_labels = [
        json.loads(line) for line in labels_path.read_text().splitlines()
    ]
    preserved = next(
        label
        for label in rebuilt_labels
        if label["review_item_id"] == reviewed["review_item_id"]
    )
    assert preserved["reviewer_label"] == "correct"
    queue = [
        json.loads(line)
        for line in (tmp_path / "review" / "review_queue.jsonl").read_text().splitlines()
    ]
    queue_item = next(
        item for item in queue if item["review_item_id"] == reviewed["review_item_id"]
    )
    assert queue_item["review_status"] == "reviewed"


def test_review_cli_refreshes_queue_status_and_metrics(tmp_path, monkeypatch):
    from harness.review_capture_session import main as review_main

    review_dir = tmp_path / "review"
    review_dir.mkdir()
    item = {
        "schema_version": REVIEW_SCHEMA,
        "review_item_id": "rv_cli",
        "session_id": "ses_test",
        "task_id": "test_task",
        "reason": "state_transition",
        "start_frame": 0,
        "end_frame": 1,
        "start_pts_ms": 0,
        "end_pts_ms": 33.33,
        "machine_label": {
            "event_type": "",
            "confidence": None,
            "predicted_step_id": "ready",
            "label_source": "test",
        },
        "review_status": "unreviewed",
        "is_ground_truth": False,
        "clip_source": "raw_video",
        "clip_path": "clips/rv_cli.mp4",
    }
    (review_dir / "review_queue.jsonl").write_text(json.dumps(item))
    (review_dir / "gold_labels.jsonl").write_text(
        json.dumps(build_gold_label_template(item))
    )
    (review_dir / "review_summary.json").write_text(
        json.dumps({"gold_labels": 0, "metrics": {}})
    )
    monkeypatch.setattr(sys, "argv", ["review_capture_session", str(tmp_path)])
    monkeypatch.setattr("builtins.input", lambda _: "c")

    review_main()

    queue_item = json.loads((review_dir / "review_queue.jsonl").read_text())
    gold_label = json.loads((review_dir / "gold_labels.jsonl").read_text())
    summary = json.loads((review_dir / "review_summary.json").read_text())
    assert queue_item["review_status"] == "reviewed"
    assert gold_label["event_type"] == "STATE_TRANSITION"
    assert summary["gold_labels"] == 1
    assert summary["metrics"]["gold_count"] == 1
