"""Review queue builder, gold label validator, and label metrics.

Schema versions:
  noma.review_item.v1  — each queue entry
  noma.gold_label.v1   — human-verified labels
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

REVIEW_SCHEMA = "noma.review_item.v1"
GOLD_SCHEMA = "noma.gold_label.v1"

CLIP_PADDING_MS = 2000
MERGE_OVERLAP_CENTER_MS = 3000  # merge if centers are this close (same reason)


def _stable_id(*parts: str) -> str:
    h = hashlib.shake_128("|".join(parts).encode()).hexdigest(8)
    return f"rv_{h}"


def _dict_items_equal(a: dict, keys: list[str], b: dict) -> bool:
    return all(a.get(k) == b.get(k) for k in keys)


def _clamp_frame(f, lo, hi):
    return max(lo, min(hi, f))


def build_review_queue(
    session_dir: str | Path,
    fps: float | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    root = Path(session_dir)
    obs_lines = _read_jsonl(root / "observations.jsonl")
    events = _read_jsonl(root / "events.jsonl")
    summary = json.loads((root / "summary.json").read_text())
    manifest = json.loads((root / "session_manifest.json").read_text())

    if fps is None:
        fps = manifest.get("fps", 30.0)
    ms_per_frame = 1000.0 / max(fps, 1.0)

    total_frames = len(obs_lines)
    max_pts = obs_lines[-1]["pts_ms"] if obs_lines else 0
    raw_video = (root / "raw_video.mp4").exists()
    annotated_video = (root / "annotated_live.mp4" if manifest.get("artifacts", {}).get("annotated_video") else Path("/nonexistent")).exists()
    clip_source = "raw_video" if raw_video else "annotated_video" if annotated_video else "none"

    def _make_clip_range(center_ms: float) -> tuple[int, int, float, float]:
        start_ms = max(0.0, center_ms - CLIP_PADDING_MS)
        end_ms = min(max_pts, center_ms + CLIP_PADDING_MS)
        start_f = _clamp_frame(int(start_ms / ms_per_frame), 0, total_frames - 1)
        end_f = _clamp_frame(int(end_ms / ms_per_frame), 0, total_frames - 1)
        return start_f, end_f, round(start_ms, 2), round(end_ms, 2)

    items: list[dict[str, Any]] = []

    # 1. state transitions
    transitions = summary.get("predicted_transitions", [])
    for t in transitions:
        step_to = t.get("step_id", "")
        pts_ms = t.get("pts_ms", 0)
        first_frame = int(t.get("first_frame", 0))
        start_f, end_f, start_ms, end_ms = _make_clip_range(pts_ms)
        items.append({
            "reason": "state_transition",
            "step_id": step_to,
            "center_frame": first_frame,
            "center_pts_ms": pts_ms,
            "start_frame": start_f, "end_frame": end_f,
            "start_pts_ms": start_ms, "end_pts_ms": end_ms,
            "fps": fps,
        })

    # 2. task completion
    if summary.get("step_status") == "completed":
        last_t = transitions[-1] if transitions else {}
        pts_ms = last_t.get("pts_ms", max_pts)
        first_frame = int(last_t.get("first_frame", 0))
        start_f, end_f, start_ms, end_ms = _make_clip_range(pts_ms)
        items.append({
            "reason": "task_completion",
            "step_id": last_t.get("step_id", ""),
            "center_frame": first_frame,
            "center_pts_ms": pts_ms,
            "start_frame": start_f, "end_frame": end_f,
            "start_pts_ms": start_ms, "end_pts_ms": end_ms,
            "fps": fps,
        })

    # 3. low-confidence evidence
    low_conf_events = [e for e in events if (e.get("confidence") or 1.0) < 0.5]
    seen_conf_centers: set[int] = set()
    for ev in low_conf_events:
        pts_ms = ev.get("t_device_ms", 0)
        frame = int(pts_ms / ms_per_frame)
        if frame in seen_conf_centers:
            continue
        seen_conf_centers.add(frame)
        start_f, end_f, start_ms, end_ms = _make_clip_range(pts_ms)
        items.append({
            "reason": "low_confidence",
            "event_type": ev.get("type", ""),
            "confidence": ev.get("confidence", 0),
            "center_frame": frame,
            "center_pts_ms": pts_ms,
            "start_frame": start_f, "end_frame": end_f,
            "start_pts_ms": start_ms, "end_pts_ms": end_ms,
            "fps": fps,
        })

    # 4. session ending without completion
    if summary.get("step_status") != "completed":
        start_f = max(0, total_frames - int(2000 / ms_per_frame))
        end_f = total_frames - 1
        items.append({
            "reason": "session_ending_incomplete",
            "step_id": summary.get("final_step_id", ""),
            "center_frame": start_f,
            "center_pts_ms": start_f * ms_per_frame,
            "start_frame": start_f, "end_frame": end_f,
            "start_pts_ms": round(start_f * ms_per_frame, 2),
            "end_pts_ms": round(end_f * ms_per_frame, 2),
            "fps": fps,
        })

    # 5. dedup and merge overlapping same-reason items by center proximity
    items.sort(key=lambda x: x["center_frame"])
    merged: list[dict[str, Any]] = []
    for item in items:
        if merged and merged[-1]["reason"] == item["reason"]:
            prev = merged[-1]
            center_dist = abs(item["center_pts_ms"] - prev["center_pts_ms"])
            if center_dist < MERGE_OVERLAP_CENTER_MS:
                prev["end_frame"] = max(prev["end_frame"], item["end_frame"])
                prev["end_pts_ms"] = max(prev["end_pts_ms"], item["end_pts_ms"])
                prev["center_frame"] = prev["center_frame"]
                continue
        merged.append(item)

    # 6. produce final review items with stable IDs
    sid = summary.get("session_id", manifest.get("session_id", "unknown"))
    task_id = manifest.get("task_id", "unknown")
    review_items = []
    for i, item in enumerate(merged):
        rid = _stable_id(sid, item["reason"], str(item["start_frame"]), str(i))
        review_items.append({
            "schema_version": REVIEW_SCHEMA,
            "review_item_id": rid,
            "session_id": sid,
            "task_id": task_id,
            "reason": item["reason"],
            "start_frame": item["start_frame"],
            "end_frame": item["end_frame"],
            "start_pts_ms": item["start_pts_ms"],
            "end_pts_ms": item["end_pts_ms"],
            "machine_label": {
                "event_type": item.get("event_type", ""),
                "confidence": item.get("confidence"),
                "predicted_step_id": item.get("step_id", ""),
                "label_source": "state_engine_and_geometry_v1",
            },
            "review_status": "unreviewed",
            "is_ground_truth": False,
            "clip_source": clip_source,
            "clip_path": f"clips/{rid}.mp4",
        })

    return review_items, {"total_frames": total_frames, "max_pts_ms": max_pts,
                           "fps": fps, "clip_source": clip_source}


def build_gold_label_template(review_item: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": GOLD_SCHEMA,
        "review_item_id": review_item["review_item_id"],
        "reviewer_label": None,
        "event_type": None,
        "step_after": None,
        "boundary_frame": None,
        "reviewer_confidence": None,
        "notes": "",
        "reviewed_at": None,
        "is_ground_truth": False,
    }


def apply_review(
    gold_label: dict[str, Any],
    *,
    reviewer_label: str,
    event_type: str | None = None,
    step_after: str | None = None,
    boundary_frame: int | None = None,
    reviewer_confidence: float | None = None,
    notes: str = "",
) -> dict[str, Any]:
    import time
    gl = dict(gold_label)
    gl["reviewer_label"] = reviewer_label
    gl["event_type"] = event_type or gl.get("event_type")
    gl["step_after"] = step_after or gl.get("step_after")
    gl["boundary_frame"] = boundary_frame or gl.get("boundary_frame")
    gl["reviewer_confidence"] = reviewer_confidence
    gl["notes"] = notes
    gl["reviewed_at"] = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc).isoformat()

    if reviewer_label in ("correct", "incorrect") and event_type is not None:
        gl["is_ground_truth"] = True
    else:
        gl["is_ground_truth"] = False

    return gl


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    result = []
    for line in path.read_text().strip().split("\n"):
        if line.strip():
            result.append(json.loads(line))
    return result


def validate_review_labels(
    session_dir: str | Path,
    review_dir: str | Path | None = None,
) -> tuple[bool, dict[str, Any]]:
    root = Path(session_dir)
    review_root = Path(review_dir) if review_dir else root / "review"

    result = {
        "queue_items": 0, "transition_items": 0, "low_confidence_items": 0,
        "completion_items": 0, "session_ending_items": 0,
        "reviewed": 0, "gold_labels": 0, "incorrect_machine_labels": 0,
        "uncertain": 0, "remaining": 0,
        "errors": [],
    }

    queue_path = review_root / "review_queue.jsonl"
    labels_path = review_root / "gold_labels.jsonl"

    if not queue_path.exists():
        result["errors"].append("review_queue.jsonl missing")
        return False, result

    queue = _read_jsonl(queue_path)
    labels = _read_jsonl(labels_path) if labels_path.exists() else []
    label_map: dict[str, dict] = {g["review_item_id"]: g for g in labels}

    seen_ids: set[str] = set()
    for item in queue:
        rid = item["review_item_id"]
        if rid in seen_ids:
            result["errors"].append(f"duplicate review_item_id: {rid}")
            continue
        seen_ids.add(rid)

        if item.get("schema_version") != REVIEW_SCHEMA:
            result["errors"].append(f"bad schema: {rid}")

        reason = item.get("reason", "")
        result["queue_items"] += 1
        if reason == "state_transition":
            result["transition_items"] += 1
        elif reason == "task_completion":
            result["completion_items"] += 1
        elif reason == "low_confidence":
            result["low_confidence_items"] += 1
        elif reason == "session_ending_incomplete":
            result["session_ending_items"] += 1

        # check clip exists
        clip = review_root / item.get("clip_path", "")
        if not clip.exists():
            result["errors"].append(f"clip missing: {clip}")

        # check range
        sf, ef = item.get("start_frame", 0), item.get("end_frame", 0)
        if sf > ef:
            result["errors"].append(f"invalid range: {rid} sf={sf} ef={ef}")
        if item.get("start_pts_ms", 0) > item.get("end_pts_ms", 0):
            result["errors"].append(f"invalid pts range: {rid}")

        # gold label checks
        gl = label_map.get(rid)
        if gl is None:
            result["remaining"] += 1
            continue

        rl = gl.get("reviewer_label")
        if rl is None:
            result["remaining"] += 1
            continue

        result["reviewed"] += 1
        if rl == "uncertain":
            result["uncertain"] += 1
            if gl.get("is_ground_truth"):
                result["errors"].append(f"uncertain marked as ground_truth: {rid}")
            continue

        if rl in ("correct", "incorrect"):
            if rl == "incorrect":
                result["incorrect_machine_labels"] += 1
            if gl.get("is_ground_truth"):
                result["gold_labels"] += 1
                if gl.get("event_type") is None:
                    result["errors"].append(f"gold label missing event_type: {rid}")
            else:
                result["errors"].append(f"{rl} but not ground_truth (missing event_type?): {rid}")

    # label ID validity
    for gl in labels:
        if gl["review_item_id"] not in seen_ids:
            result["errors"].append(f"orphan gold label: {gl['review_item_id']}")

    # check secrets
    for path in (queue_path, labels_path):
        if path.exists():
            from server.data.capture import secrets_check
            try:
                secrets_check(path.read_text())
            except ValueError as e:
                result["errors"].append(str(e))

    passed = len(result["errors"]) == 0
    return passed, result


def build_label_metrics(
    session_dir: str | Path,
    review_dir: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(session_dir)
    review_root = Path(review_dir) if review_dir else root / "review"

    queue = _read_jsonl(review_root / "review_queue.jsonl")
    labels = _read_jsonl(review_root / "gold_labels.jsonl") if (review_root / "gold_labels.jsonl").exists() else []
    label_map = {g["review_item_id"]: g for g in labels}

    golds = [g for g in labels if g.get("is_ground_truth")]
    reviewed = [g for g in labels if g.get("reviewer_label") is not None]
    corrects = [g for g in golds if g.get("reviewer_label") == "correct"]
    incorrects = [g for g in golds if g.get("reviewer_label") == "incorrect"]
    uncerts = [g for g in labels if g.get("reviewer_label") == "uncertain"]

    by_event: dict[str, dict] = {}
    for g in golds:
        et = g.get("event_type", "unknown")
        if et not in by_event:
            by_event[et] = {"total": 0, "correct": 0, "incorrect": 0}
        by_event[et]["total"] += 1
        if g.get("reviewer_label") == "correct":
            by_event[et]["correct"] += 1
        elif g.get("reviewer_label") == "incorrect":
            by_event[et]["incorrect"] += 1

    accuracy = round(len(corrects) / max(len(golds), 1), 4) if golds else None

    boundary_errors = []
    for g in golds:
        bf = g.get("boundary_frame")
        if bf is not None:
            item = next((i for i in queue if i["review_item_id"] == g["review_item_id"]), None)
            if item:
                boundary_errors.append(abs(bf - item.get("center_frame", 0)))

    return {
        "queue_total": len(queue),
        "reviewed": len(reviewed),
        "gold_count": len(golds),
        "correct_machine_predictions": len(corrects),
        "incorrect_machine_predictions": len(incorrects),
        "uncertain_count": len(uncerts),
        "accuracy_on_reviewed": accuracy,
        "mean_boundary_error_frames": round(sum(boundary_errors) / len(boundary_errors), 2) if boundary_errors else None,
        "by_event_type": by_event,
    }
