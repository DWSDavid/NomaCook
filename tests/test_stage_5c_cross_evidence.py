"""Tests for the deterministic cross-evidence evaluator and VLM shadow trigger."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.data.cross_evidence import (
    EVAL_SCHEMA,
    MIN_GOLD_SAMPLE,
    align_review_item,
    aggregate_sessions,
    build_report,
    collect_window_evidence,
    determine_vlm_trigger,
    detect_occlusion,
    evaluate_session,
    make_contact_sheet,
    run_shadow_evaluation,
    sample_frames,
)


def _write_session(tmp_path, golds=0, uncertain=0, n_items=0, completed=True):
    """Write a minimal session with review queue + gold labels + events + observations + summary."""
    root = tmp_path
    review_dir = root / "review"
    review_dir.mkdir()
    clips_dir = review_dir / "clips"
    clips_dir.mkdir()

    queue = []
    labels = []
    for i in range(n_items):
        rid = f"rv_item_{i}"
        reason = "state_transition" if i % 3 == 0 else "low_confidence"
        item = {
            "schema_version": "noma.review_item.v1",
            "review_item_id": rid,
            "session_id": "ses_x",
            "task_id": "t1",
            "reason": reason,
            "start_frame": i * 10,
            "end_frame": i * 10 + 9,
            "start_pts_ms": i * 300,
            "end_pts_ms": i * 300 + 297,
            "machine_label": {
                "event_type": "OBJECT_PRESENT" if reason == "low_confidence" else "",
                "confidence": 0.4 if reason == "low_confidence" else None,
                "predicted_step_id": f"step_{i}",
                "label_source": "state_engine_and_geometry_v1",
            },
            "review_status": "unreviewed",
            "is_ground_truth": False,
            "clip_path": f"clips/{rid}.mp4",
            "clip_source": "raw_video",
        }
        queue.append(item)

        # gold label
        if i < golds:
            label = {
                "schema_version": "noma.gold_label.v1",
                "review_item_id": rid,
                "reviewer_label": "correct",
                "event_type": "EV",
                "step_after": None,
                "boundary_frame": None,
                "reviewer_confidence": 1.0,
                "notes": "",
                "reviewed_at": "2026-01-01T00:00:00Z",
                "is_ground_truth": True,
            }
        elif i < golds + uncertain:
            label = {
                "schema_version": "noma.gold_label.v1",
                "review_item_id": rid,
                "reviewer_label": "uncertain",
                "event_type": None,
                "step_after": None,
                "boundary_frame": None,
                "reviewer_confidence": None,
                "notes": "",
                "reviewed_at": "2026-01-01T00:00:00Z",
                "is_ground_truth": False,
            }
        else:
            label = {
                "schema_version": "noma.gold_label.v1",
                "review_item_id": rid,
                "reviewer_label": None,
                "event_type": None,
                "step_after": None,
                "boundary_frame": None,
                "reviewer_confidence": None,
                "notes": "",
                "reviewed_at": None,
                "is_ground_truth": False,
            }
        labels.append(label)

    (review_dir / "review_queue.jsonl").write_text(
        "\n".join(json.dumps(i) for i in queue))
    (review_dir / "gold_labels.jsonl").write_text(
        "\n".join(json.dumps(g) for g in labels))

    # events + observations within window
    events = [
        {"type": "OBJECT_PRESENT", "t_device_ms": i * 300 + 100,
         "source": "s1", "confidence": 0.8}
        for i in range(n_items)
    ]
    observations = [
        {"pts_ms": i * 300 + 100, "machine_labels": {"current_step_id": f"step_{i}"},
         "detections": [{"label": "tomato"}], "hands": []}
        for i in range(n_items)
    ]
    (root / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events))
    (root / "observations.jsonl").write_text("\n".join(json.dumps(o) for o in observations))
    summary = {
        "session_id": "ses_x",
        "final_step_id": "step_end",
        "step_status": "completed" if completed else "in_progress",
        "predicted_transitions": [],
    }
    (root / "summary.json").write_text(json.dumps(summary))
    return root


# ── collect_window_evidence: inclusive boundaries ──

def test_collect_window_evidence_inclusive_bounds():
    events = [
        {"type": "A", "t_device_ms": 100.0, "source": "s1", "confidence": 0.9},
        {"type": "B", "t_device_ms": 300.0, "source": "s2", "confidence": 0.5},
        {"type": "C", "t_device_ms": 301.0, "source": "s3", "confidence": 0.6},
    ]
    obs = [
        {"pts_ms": 100.0, "machine_labels": {"current_step_id": "s1"},
         "detections": [{"label": "tomato"}], "hands": []},
    ]
    ev = collect_window_evidence(events, obs, 100.0, 300.0)
    assert ev["event_types"] == ["A", "B"]
    assert "C" not in ev["event_types"]
    assert ev["detection_labels"] == ["tomato"]


# ── composite identity (session_directory + review_item_id) ──

def test_composite_identity_in_records(tmp_path):
    root = _write_session(tmp_path, golds=1, n_items=2)
    res = evaluate_session(root)
    for rec in res["records"]:
        assert rec["session_directory"] == str(root)
        assert "review_item_id" in rec
    # identity = (session_directory, review_item_id) is implicit in the record


# ── gold-only accuracy + uncertain exclusion ──

def test_gold_only_accuracy_excludes_uncertain(tmp_path):
    root = _write_session(tmp_path, golds=2, uncertain=1, n_items=5)
    res = evaluate_session(root)
    assert len(res["gold_items"]) == 2
    assert len(res["ambiguity_candidates"]) == 1

    agg = aggregate_sessions([res])
    assert agg["gold_count"] == 2
    assert agg["uncertain_count"] == 1
    # 2 golds < MIN_GOLD_SAMPLE → null + insufficient
    assert agg["accuracy"] is None
    assert agg["sample_status"] == "insufficient_sample"


def test_sample_status_sufficient_when_enough_golds(tmp_path):
    root = _write_session(tmp_path, golds=MIN_GOLD_SAMPLE, n_items=MIN_GOLD_SAMPLE + 2)
    res = evaluate_session(root)
    agg = aggregate_sessions([res])
    assert agg["gold_count"] == MIN_GOLD_SAMPLE
    assert agg["accuracy"] == 1.0
    assert agg["sample_status"] == "sufficient_sample"


# ── report schema ──

def test_report_schema_version(tmp_path):
    root = _write_session(tmp_path, golds=1, n_items=2)
    report = build_report([evaluate_session(root)])
    assert report["schema_version"] == EVAL_SCHEMA
    assert report["metrics"]["accuracy"] is None


# ── detect_occlusion ──

def test_detect_occlusion_true():
    obs = [
        {"pts_ms": 100.0, "detections": [{"label": "tomato"}]},
        {"pts_ms": 150.0, "detections": []},
        {"pts_ms": 200.0, "detections": [{"label": "tomato"}]},
    ]
    assert detect_occlusion(obs, 0, 300) is True


def test_detect_occlusion_false_when_continuous():
    obs = [
        {"pts_ms": 100.0, "detections": [{"label": "tomato"}]},
        {"pts_ms": 150.0, "detections": [{"label": "tomato"}]},
    ]
    assert detect_occlusion(obs, 0, 300) is False


# ── determine_vlm_trigger ──

def test_vlm_trigger_uncertain():
    item = {"reason": "conflict", "start_pts_ms": 0, "end_pts_ms": 300}
    gl = {"reviewer_label": "uncertain"}
    trigger = determine_vlm_trigger(item, gl, [])
    assert trigger is not None
    assert trigger[0] == "uncertain"


def test_vlm_trigger_completion():
    item = {"reason": "task_completion", "start_pts_ms": 0, "end_pts_ms": 300}
    trigger = determine_vlm_trigger(item, None, [])
    assert trigger is not None
    assert trigger[0] == "completion"


def test_vlm_trigger_session_ending():
    item = {"reason": "session_ending_incomplete", "start_pts_ms": 0, "end_pts_ms": 300}
    trigger = determine_vlm_trigger(item, None, [])
    assert trigger is not None
    assert trigger[0] == "session_ending"


def test_vlm_trigger_no_trigger_for_plain_transition():
    item = {"reason": "state_transition", "start_pts_ms": 0, "end_pts_ms": 300}
    trigger = determine_vlm_trigger(item, None, [])
    assert trigger is None


# ── sample_frames + contact sheet ──

def test_sample_frames_and_contact_sheet(tmp_path):
    import cv2
    import numpy as np
    video = tmp_path / "raw_video.mp4"
    out = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (64, 64))
    for _ in range(10):
        out.write(np.zeros((64, 64, 3), dtype=np.uint8))
    out.release()

    frames = sample_frames(video, 0, 9, count=3)
    assert len(frames) == 3

    sheet = make_contact_sheet(frames)
    assert len(sheet) > 0


# ── secrets not in output ──

def test_report_contains_no_secrets(tmp_path):
    root = _write_session(tmp_path, golds=1, n_items=1)
    report = build_report([evaluate_session(root)])
    dumped = json.dumps(report)
    assert "sk-ws" not in dumped
    assert "DASHSCOPE_API_KEY" not in dumped


# ── VLM shadow with fake client (no network) ──

class _FakeVLMClient:
    def __init__(self, answer="yes", confidence=0.9):
        self._answer = answer
        self._confidence = confidence
        self.calls = 0

    def analyze_contact_sheet(self, question, image_bytes):
        self.calls += 1
        return {"answer": self._answer, "confidence": self._confidence}


def test_run_shadow_evaluation_with_fake_client(tmp_path):
    """Fake client proves trigger → contact sheet → record without network."""
    root = _write_session(tmp_path, golds=1, n_items=3, completed=True)
    # add a raw video
    import cv2
    import numpy as np
    video = root / "raw_video.mp4"
    out = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (64, 64))
    for _ in range(30):
        out.write(np.zeros((64, 64, 3), dtype=np.uint8))
    out.release()

    # write a conflict item into the real review_queue.jsonl so the trigger fires
    review_dir = root / "review"
    queue = [json.loads(l) for l in (review_dir / "review_queue.jsonl").read_text().splitlines()]
    queue.append({
        "schema_version": "noma.review_item.v1",
        "review_item_id": "rv_conflict",
        "session_id": "ses_x", "task_id": "t1",
        "reason": "conflict",
        "start_frame": 0, "end_frame": 29,
        "start_pts_ms": 0, "end_pts_ms": 2900,
        "machine_label": {"event_type": "HAND_NEAR_STARTED|HAND_NEAR_ENDED",
                           "confidence": None, "predicted_step_id": "",
                           "label_source": "state_engine_and_geometry_v1"},
        "review_status": "unreviewed", "is_ground_truth": False,
        "clip_path": "clips/rv_conflict.mp4", "clip_source": "raw_video",
    })
    (review_dir / "review_queue.jsonl").write_text(
        "\n".join(json.dumps(i) for i in queue))

    session_result = evaluate_session(root)

    fake = _FakeVLMClient()
    records = run_shadow_evaluation([session_result], fake)

    # fake client was called at least once (conflict trigger existed)
    assert fake.calls >= 1
    for r in records:
        assert r["status"] in ("executed", "skipped")
        if r["status"] == "executed":
            assert r["answer"] in ("yes", "no")
            assert r["confidence"] is not None
            assert "question" in r


def test_run_shadow_evaluation_never_writes_event_log(tmp_path):
    root = _write_session(tmp_path, golds=1, n_items=3, completed=False)
    import cv2
    import numpy as np
    video = root / "raw_video.mp4"
    out = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (64, 64))
    for _ in range(30):
        out.write(np.zeros((64, 64, 3), dtype=np.uint8))
    out.release()

    session_result = evaluate_session(root)
    events_before = (root / "events.jsonl").read_text()

    fake = _FakeVLMClient()
    run_shadow_evaluation([session_result], fake)

    # events.jsonl unchanged (no VLM output leaked into live event log)
    assert (root / "events.jsonl").read_text() == events_before
