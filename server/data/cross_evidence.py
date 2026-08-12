"""Deterministic cross-evidence evaluator for human-reviewed capture items.

Aligns review items against events.jsonl and observations.jsonl within each
item's inclusive time window. Correctness metrics use only is_ground_truth=true
Gold Labels. Never modifies StateEngine or the live event log.

Schema version: noma.cross_evidence_eval.v1
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .review import _read_jsonl

EVAL_SCHEMA = "noma.cross_evidence_eval.v1"
MIN_GOLD_SAMPLE = 5

CONFLICT_PAIRS = (
    frozenset(("HAND_NEAR_STARTED", "HAND_NEAR_ENDED")),
    frozenset(("HOLDING_STARTED", "HOLDING_ENDED")),
    frozenset(("OBJECT_ENTERED_REGION", "OBJECT_EXITED_REGION")),
)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text()) if path.exists() else {}


def _in_window(pts_ms: float, start: float, end: float) -> bool:
    return start <= pts_ms <= end


def collect_window_evidence(
    events: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    start_pts_ms: float,
    end_pts_ms: float,
) -> dict[str, Any]:
    """Collect event types, sources, confidences, detections, hands, and step IDs
    whose timestamps fall inside the inclusive window."""
    event_types: set[str] = set()
    event_sources: set[str] = set()
    confidences: list[float] = []
    detection_labels: set[str] = set()
    hand_frames = 0
    gripping_hand_frames = 0
    step_ids: set[str] = set()

    for ev in events:
        pts = ev.get("t_device_ms", 0.0)
        if _in_window(pts, start_pts_ms, end_pts_ms):
            event_types.add(ev.get("type", ""))
            event_sources.add(ev.get("source", ""))
            conf = ev.get("confidence")
            if conf is not None and math.isfinite(conf):
                confidences.append(float(conf))

    for obs in observations:
        pts = obs.get("pts_ms", 0.0)
        if _in_window(pts, start_pts_ms, end_pts_ms):
            ml = obs.get("machine_labels", {})
            step = ml.get("current_step_id")
            if step:
                step_ids.add(step)
            for det in obs.get("detections", []):
                label = det.get("label")
                if label:
                    detection_labels.add(label)
            hands = obs.get("hands", [])
            if hands:
                hand_frames += 1
                if any(h.get("grip_closure", 0.0) > 0.55 for h in hands):
                    gripping_hand_frames += 1

    conflicts: list[str] = []
    for pair in CONFLICT_PAIRS:
        if pair <= event_types:
            conflicts.append("|".join(sorted(pair)))

    return {
        "event_types": sorted(event_types),
        "event_sources": sorted(event_sources),
        "min_confidence": round(min(confidences), 4) if confidences else None,
        "max_confidence": round(max(confidences), 4) if confidences else None,
        "detection_labels": sorted(detection_labels),
        "hand_frames": hand_frames,
        "gripping_hand_frames": gripping_hand_frames,
        "step_ids": sorted(step_ids),
        "evidence_conflicts": conflicts,
    }


def align_review_item(
    *,
    session_dir: str,
    review_item: dict[str, Any],
    gold_label: dict[str, Any] | None,
    events: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    summary: dict[str, Any],
) -> dict[str, Any]:
    """Align one review item with its gold label and evidence streams."""
    rid = review_item["review_item_id"]
    start_pts = review_item.get("start_pts_ms", 0.0)
    end_pts = review_item.get("end_pts_ms", 0.0)
    evidence = collect_window_evidence(events, observations, start_pts, end_pts)

    record: dict[str, Any] = {
        "session_directory": session_dir,
        "review_item_id": rid,
        "reason": review_item.get("reason", ""),
        "start_pts_ms": start_pts,
        "end_pts_ms": end_pts,
        "machine_prediction": review_item.get("machine_label", {}),
        "gold_decision": gold_label,
        "evidence": evidence,
        "final_session_state": {
            "final_step_id": summary.get("final_step_id"),
            "step_status": summary.get("step_status"),
        },
    }

    if gold_label is None:
        record["correctness_eligibility"] = "unreviewed"
        return record

    reviewer_label = gold_label.get("reviewer_label")
    is_gt = bool(gold_label.get("is_ground_truth"))

    if reviewer_label == "uncertain":
        record["correctness_eligibility"] = "ambiguous"
    elif is_gt and reviewer_label in ("correct", "incorrect"):
        record["correctness_eligibility"] = "gold"
        record["gold_correct"] = reviewer_label == "correct"
    else:
        record["correctness_eligibility"] = "excluded"

    return record


def evaluate_session(session_dir: str | Path) -> dict[str, Any]:
    """Produce one deterministic evaluation record for a session directory."""
    root = Path(session_dir)
    review_dir = root / "review"
    queue = _read_jsonl(review_dir / "review_queue.jsonl")
    labels = _read_jsonl(review_dir / "gold_labels.jsonl")
    label_map = {g["review_item_id"]: g for g in labels if g.get("review_item_id")}

    events = _read_jsonl(root / "events.jsonl")
    observations = _read_jsonl(root / "observations.jsonl")
    summary = _read_json(root / "summary.json")

    records = []
    gold_items = []
    ambiguous = []
    for item in queue:
        rid = item["review_item_id"]
        gl = label_map.get(rid)
        record = align_review_item(
            session_dir=str(root),
            review_item=item,
            gold_label=gl,
            events=events,
            observations=observations,
            summary=summary,
        )
        records.append(record)
        if record["correctness_eligibility"] == "gold":
            gold_items.append(record)
        elif record["correctness_eligibility"] == "ambiguous":
            ambiguous.append(record)

    return {
        "session_directory": str(root),
        "session_id": summary.get("session_id"),
        "gold_items": gold_items,
        "ambiguity_candidates": ambiguous,
        "records": records,
    }


def aggregate_sessions(session_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate gold-only accuracy, ambiguity counts, and sample sufficiency."""
    gold_items = [r for s in session_results for r in s["gold_items"]]
    ambiguous = [r for s in session_results for r in s["ambiguity_candidates"]]

    correct = sum(1 for r in gold_items if r.get("gold_correct"))
    incorrect = sum(1 for r in gold_items if not r.get("gold_correct"))
    gold_count = len(gold_items)

    if gold_count >= MIN_GOLD_SAMPLE:
        accuracy = round(correct / gold_count, 4)
        sample_status = "sufficient_sample"
    else:
        accuracy = None
        sample_status = "insufficient_sample"

    return {
        "gold_count": gold_count,
        "correct": correct,
        "incorrect": incorrect,
        "uncertain_count": len(ambiguous),
        "accuracy": accuracy,
        "sample_status": sample_status,
    }


def build_report(
    session_results: list[dict[str, Any]],
    *,
    vlm_shadow: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    agg = aggregate_sessions(session_results)
    return {
        "schema_version": EVAL_SCHEMA,
        "evaluated_sessions": [
            {"session_directory": s["session_directory"], "session_id": s["session_id"]}
            for s in session_results
        ],
        "metrics": agg,
        "sessions": session_results,
        "vlm_shadow": vlm_shadow or [],
        "limitations": [
            "Historical Stage 5A sessions share one legacy session_id; "
            "review_item_id alone is unsafe across sessions.",
            "Current Gold sample is pilot-sized; accuracy is null until at least "
            f"{MIN_GOLD_SAMPLE} Gold items exist per group.",
            "Machine labels are weak/silver; only human-confirmed Gold Labels "
            "enter correctness metrics.",
        ],
    }


# ── VLM shadow trigger + frame sampling ──

VLM_TRIGGER_QUESTIONS = {
    "uncertain": "Across these frames, is the person continuously holding the tomato?",
    "conflict": "Across these frames, is the person continuously holding the tomato?",
    "completion": "Was the tomato released inside the refrigerator?",
    "session_ending": "Is the tomato visibly present in the scene?",
}


def detect_occlusion(
    observations: list[dict[str, Any]],
    start_pts_ms: float,
    end_pts_ms: float,
    label: str = "tomato",
) -> bool:
    """Detect present → absent → present for a label within the window."""
    seen_present = False
    seen_absent_after_present = False
    for obs in observations:
        pts = obs.get("pts_ms", 0.0)
        if not _in_window(pts, start_pts_ms, end_pts_ms):
            continue
        present = any(d.get("label") == label for d in obs.get("detections", []))
        if present:
            if seen_absent_after_present:
                return True
            seen_present = True
        else:
            if seen_present:
                seen_absent_after_present = True
    return False


def determine_vlm_trigger(
    review_item: dict[str, Any],
    gold_label: dict[str, Any] | None,
    observations: list[dict[str, Any]],
) -> tuple[str, str] | None:
    """Return (trigger_reason, question) if a shadow VLM call is warranted."""
    reason = review_item.get("reason", "")

    if gold_label is not None and gold_label.get("reviewer_label") == "uncertain":
        return "uncertain", VLM_TRIGGER_QUESTIONS["uncertain"]

    if "conflict" in reason:
        return "conflict", VLM_TRIGGER_QUESTIONS["conflict"]

    if reason == "task_completion":
        return "completion", VLM_TRIGGER_QUESTIONS["completion"]

    if reason == "session_ending_incomplete":
        return "session_ending", VLM_TRIGGER_QUESTIONS["session_ending"]

    if detect_occlusion(
        observations, review_item.get("start_pts_ms", 0.0),
        review_item.get("end_pts_ms", 0.0),
    ):
        return "occlusion", VLM_TRIGGER_QUESTIONS["uncertain"]

    return None


def sample_frames(
    video_path: Path,
    start_frame: int,
    end_frame: int,
    *,
    count: int = 3,
) -> list[tuple[int, Any]]:
    """Extract `count` representative frames (first/mid/last) as BGR arrays."""
    import cv2

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        cap.release()
        return []

    if end_frame <= start_frame:
        indices = [start_frame] * count
    else:
        step = (end_frame - start_frame) / (count - 1)
        indices = [int(start_frame + i * step) for i in range(count)]

    frames: list[tuple[int, Any]] = []
    for idx in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if ok:
            frames.append((idx, frame))
    cap.release()
    return frames


def make_contact_sheet(frames: list[tuple[int, Any]], *, max_w: int = 480) -> bytes:
    """Compose sampled frames into one horizontal JPEG contact sheet."""
    import cv2

    if not frames:
        return b""

    resized = []
    for _, frame in frames:
        h, w = frame.shape[:2]
        scale = max_w / w
        resized.append(cv2.resize(frame, (max_w, max(1, int(h * scale)))))

    sheet = None
    for frame in resized:
        if sheet is None:
            sheet = frame
        else:
            sheet = cv2.hconcat([sheet, frame])

    ok, encoded = cv2.imencode(".jpg", sheet, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return encoded.tobytes() if ok else b""


def run_shadow_evaluation(
    session_results: list[dict[str, Any]],
    client,
) -> list[dict[str, Any]]:
    """Run offline VLM shadow cross-check using an injected client.

    The client must expose `analyze_contact_sheet(question, image_bytes) -> dict`.
    This function never touches live state or the event log. In tests, pass a
    fake client so no network access occurs.
    """
    from .review import _read_jsonl

    records: list[dict[str, Any]] = []
    for session in session_results:
        root = Path(session["session_directory"])
        review_dir = root / "review"
        queue = _read_jsonl(review_dir / "review_queue.jsonl")
        labels = _read_jsonl(review_dir / "gold_labels.jsonl")
        label_map = {g["review_item_id"]: g for g in labels if g.get("review_item_id")}
        observations = _read_jsonl(root / "observations.jsonl")
        video = root / "raw_video.mp4"
        if not video.exists():
            video = root / "annotated_live.mp4"

        for item in queue:
            rid = item["review_item_id"]
            gl = label_map.get(rid)
            trigger = determine_vlm_trigger(item, gl, observations)
            if trigger is None:
                continue
            trigger_reason, question = trigger

            if not video.exists():
                records.append({
                    "status": "skipped", "review_item_id": rid,
                    "trigger": trigger_reason, "reason": "no video source",
                })
                continue

            frames = sample_frames(video, item["start_frame"], item["end_frame"], count=3)
            sheet = make_contact_sheet(frames)
            if not sheet:
                records.append({
                    "status": "skipped", "review_item_id": rid,
                    "trigger": trigger_reason, "reason": "contact sheet empty",
                })
                continue

            answer = client.analyze_contact_sheet(question, sheet)
            gold = gl if (gl and gl.get("is_ground_truth")) else None

            records.append({
                "status": "executed",
                "review_item_id": rid,
                "session_directory": session["session_directory"],
                "trigger": trigger_reason,
                "question": question,
                "sampled_timestamps": [f[0] for f in frames],
                "answer": answer.get("answer"),
                "confidence": answer.get("confidence"),
                "gold_comparison_eligible": gold is not None,
                "agreement_with_gold": None,
            })

    return records
