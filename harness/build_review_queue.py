"""Build review queue from a capture session.

Usage:
  .venv/bin/python -m harness.build_review_queue <session_dir> [--review-dir <dir>]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.data.capture import secrets_check

import server.data.review as rv
from server.data.review import build_gold_label_template, build_review_queue, build_label_metrics


def _cut_clip(
    source_video: Path,
    out_path: Path,
    start_frame: int,
    end_frame: int,
) -> bool:
    cap = cv2.VideoCapture(str(source_video))
    if not cap.isOpened():
        return False
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fourcc = cv2.VideoWriter_fourcc(*"avc1")
    out = cv2.VideoWriter(str(out_path), fourcc, fps, (w, h))
    if not out.isOpened():
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        out = cv2.VideoWriter(str(out_path), fourcc, fps, (w, h))
    if not out.isOpened():
        cap.release()
        return False

    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
    for fi in range(start_frame, end_frame + 1):
        ok, frame = cap.read()
        if not ok:
            break
        out.write(frame)
    cap.release()
    out.release()
    return True


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python -m harness.build_review_queue <session_dir> [--review-dir <dir>]",
              file=sys.stderr)
        sys.exit(2)

    session_dir = Path(sys.argv[1])
    if not session_dir.is_dir():
        print(f"Not a directory: {session_dir}", file=sys.stderr)
        sys.exit(2)

    review_dir = session_dir / "review"
    for i in range(1, len(sys.argv)):
        if sys.argv[i] == "--review-dir" and i + 1 < len(sys.argv):
            review_dir = Path(sys.argv[i + 1])

    review_dir.mkdir(parents=True, exist_ok=True)
    clips_dir = review_dir / "clips"
    clips_dir.mkdir(exist_ok=True)

    # secret check
    for fname in ("observations.jsonl", "events.jsonl"):
        p = session_dir / fname
        if p.exists():
            secrets_check(p.read_text())

    # build review items
    items, meta = build_review_queue(session_dir)
    clip_source = meta["clip_source"]
    source_video = session_dir / f"{'raw_video' if clip_source == 'raw_video' else 'annotated_video'}.mp4"
    if clip_source == "raw_video":
        source_video = session_dir / "raw_video.mp4"
    elif clip_source == "annotated_video":
        source_video = session_dir / "annotated_live.mp4"

    if clip_source == "none":
        # fallback: try both
        for vf in ("raw_video.mp4", "annotated_live.mp4"):
            if (session_dir / vf).exists():
                source_video = session_dir / vf
                clip_source = "raw_video" if vf == "raw_video.mp4" else "annotated_video"
                break

    queue_path = review_dir / "review_queue.jsonl"
    labels_path = review_dir / "gold_labels.jsonl"
    existing_labels = {
        label["review_item_id"]: label
        for label in rv._read_jsonl(labels_path)
        if label.get("review_item_id")
    }
    for item in items:
        label = existing_labels.get(item["review_item_id"])
        if label and label.get("reviewer_label") is not None:
            item["review_status"] = "reviewed"

    # write queue
    with queue_path.open("w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    # preserve completed reviews when regenerating deterministic queue items
    with labels_path.open("w", encoding="utf-8") as f:
        for item in items:
            gl = existing_labels.get(
                item["review_item_id"], build_gold_label_template(item)
            )
            f.write(json.dumps(gl, ensure_ascii=False) + "\n")

    # cut clips
    clip_count = 0
    for item in items:
        rid = item["review_item_id"]
        clip_path = clips_dir / f"{rid}.mp4"
        if _cut_clip(source_video, clip_path, item["start_frame"], item["end_frame"]):
            clip_count += 1

    # write summary
    metrics = build_label_metrics(session_dir, review_dir)

    # capture quality counters
    total_hand_dets = 0
    valid_21lm_hands = 0
    dropped_invalid_hands = 0
    frames_with_valid = 0
    frames_no_valid = 0
    frames_with_dets = 0
    for obs_path in [session_dir / "observations.jsonl"]:
        if not obs_path.exists():
            continue
        for line in obs_path.read_text().strip().split("\n"):
            if not line.strip():
                continue
            obs = json.loads(line)
            hands = obs.get("hands", [])
            total_hand_dets += len(hands)
            has_valid = False
            for h in hands:
                if len(h.get("landmarks_norm", [])) == 21:
                    valid_21lm_hands += 1
                    has_valid = True
                else:
                    dropped_invalid_hands += 1
            if has_valid:
                frames_with_valid += 1
            elif hands:
                frames_no_valid += 1
            if obs.get("detections"):
                frames_with_dets += 1

    total_frames = 0
    for obs_path in [session_dir / "observations.jsonl"]:
        if obs_path.exists():
            total_frames = len(obs_path.read_text().strip().split("\n"))

    detector_coverage = round(frames_with_dets / max(total_frames, 1), 4) if total_frames else 0
    hand_coverage = round(frames_with_valid / max(total_frames, 1), 4) if total_frames else 0

    summary_path = review_dir / "review_summary.json"
    previous_summary = (
        json.loads(summary_path.read_text()) if summary_path.exists() else {}
    )
    summary = {
        "session_id": items[0]["session_id"] if items else "",
        "task_id": items[0]["task_id"] if items else "",
        "clip_source": clip_source,
        "queue_items": len(items),
        "transition_items": sum(1 for i in items if i["reason"] == "state_transition"),
        "completion_items": sum(1 for i in items if i["reason"] == "task_completion"),
        "low_confidence_items": sum(1 for i in items if i["reason"] == "low_confidence"),
        "conflict_items": sum(1 for i in items if i["reason"] == "conflict"),
        "session_ending_items": sum(1 for i in items if i["reason"] == "session_ending_incomplete"),
        "clips_generated": clip_count,
        "gold_labels": metrics["gold_count"],
        "human_acceptance": previous_summary.get("human_acceptance", "PENDING"),
        "capture_quality": {
            "total_frames": total_frames,
            "total_hand_detections": total_hand_dets,
            "valid_21_landmark_hands": valid_21lm_hands,
            "dropped_invalid_hands": dropped_invalid_hands,
            "frames_with_valid_hands": frames_with_valid,
            "frames_without_valid_hands": frames_no_valid,
            "detector_coverage": detector_coverage,
            "hand_coverage": hand_coverage,
        },
        "metrics": metrics,
    }
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False))

    # validate
    v_passed, v_result = rv.validate_review_labels(session_dir, review_dir)
    print(f"Review Queue: {'PASS' if v_passed else 'FAIL'}")
    print(f"Queue items: {len(items)}")
    print(f"  transitions: {summary['transition_items']}")
    print(f"  completions: {summary['completion_items']}")
    print(f"  low-confidence: {summary['low_confidence_items']}")
    print(f"  conflicts: {summary['conflict_items']}")
    print(f"  session-ending: {summary['session_ending_items']}")
    print(f"Clips: {clip_count}/{len(items)}")
    print(f"Clip source: {clip_source}")
    print(f"Gold labels: {summary['gold_labels']}")
    print(f"Human acceptance: {summary['human_acceptance']}")
    q = summary["capture_quality"]
    print(f"Capture quality:")
    print(f"  Total hand detections: {q['total_hand_detections']}")
    print(f"  Valid 21-landmark: {q['valid_21_landmark_hands']}")
    print(f"  Dropped invalid: {q['dropped_invalid_hands']}")
    print(f"  Detector coverage: {q['detector_coverage']}")
    print(f"  Hand coverage: {q['hand_coverage']}")

    if not v_passed:
        for e in v_result["errors"][:10]:
            print(f"  ERROR: {e}")

    sys.exit(0 if v_passed else 1)


if __name__ == "__main__":
    main()
