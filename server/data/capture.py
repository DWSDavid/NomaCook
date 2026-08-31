"""Frame observation builder, session manifest writer, and capture validator.

Schema version: noma.frame_observation.v1
All labels are machine-generated weak labels (never ground truth).
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

SCHEMA_VERSION = "noma.frame_observation.v1"
MANIFEST_SCHEMA = "noma.capture_session.v1"


def build_frame_observation(
    *,
    session_id: str,
    seq_no: int,
    frame_idx: int,
    pts_ms: float,
    frame_width: int,
    frame_height: int,
    inference_ran: bool,
    detected_objects: list[tuple[str, float, tuple[int, int, int, int]]] | None = None,
    hands: list[Any] | None = None,
    current_step_id: str = "",
    step_status: str = "",
    context_version: int = 0,
    emitted_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    def _norm(x, y):
        return [round(float(x) / max(frame_width, 1), 4),
                round(float(y) / max(frame_height, 1), 4)]

    def _is_finite(v):
        return v is not None and math.isfinite(v)

    # detections
    dets = []
    if detected_objects:
        for label, conf, box in detected_objects:
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            dets.append({
                "label": label,
                "confidence": round(float(conf), 4),
                "box_xyxy_px": [int(box[0]), int(box[1]), int(box[2]), int(box[3])],
                "center_norm": _norm(cx, cy),
            })

    # hands
    hand_records = []
    if hands:
        for h in hands:
            lms = getattr(h, "landmarks_px", None)
            if lms is None or len(lms) != 21:
                continue
            lm_norm = []
            for pt in lms:
                x, y = float(pt[0]), float(pt[1])
                if not _is_finite(x) or not _is_finite(y):
                    x, y = 0.0, 0.0
                lm_norm.append(_norm(x, y))
            pc = getattr(h, "palm_center", (0.0, 0.0))
            box = getattr(h, "box", (0, 0, 0, 0))
            gc = float(getattr(h, "grip_closure", 0.0))
            if not _is_finite(gc):
                gc = 0.0
            hand_records.append({
                "handedness": getattr(h, "handedness", "Unknown"),
                "box_xyxy_px": [int(box[0]), int(box[1]), int(box[2]), int(box[3])],
                "palm_center_norm": _norm(pc[0], pc[1]),
                "grip_closure": round(gc, 4),
                "landmarks_norm": lm_norm,
            })

    # machine labels
    events = []
    if emitted_events:
        for ev in emitted_events:
            events.append({"type": ev.get("type", ev if isinstance(ev, str) else "?"),
                            "confidence": round(float(ev.get("confidence", 1.0)), 4)})

    return {
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "frame_idx": frame_idx,
        "pts_ms": round(float(pts_ms), 2),
        "frame_width": frame_width,
        "frame_height": frame_height,
        "inference_ran": inference_ran,
        "detections": dets,
        "hands": hand_records,
        "machine_labels": {
            "current_step_id": current_step_id,
            "step_status": step_status,
            "context_version": context_version,
            "emitted_events": events,
            "label_source": "state_engine_and_geometry_v1",
            "is_ground_truth": False,
        },
    }


def build_session_manifest(
    *,
    session_id: str,
    task_id: str,
    source: str,
    source_type: str,
    frame_width: int,
    frame_height: int,
    fps: float,
    detect_every: int,
    started_at: float,
    ended_at: float,
    session_dir: Path,
    raw_video: bool = False,
    annotated_video: bool = False,
) -> dict[str, Any]:
    artifacts: dict[str, str | None] = {}
    for name, fname in [
        ("observations", "observations.jsonl"),
        ("events", "events.jsonl"),
        ("snapshots", "snapshots.jsonl"),
        ("summary", "summary.json"),
        ("annotated_video", "annotated_live.mp4" if annotated_video else None),
        ("raw_video", "raw_video.mp4" if raw_video else None),
    ]:
        if fname and (session_dir / fname).exists():
            artifacts[name] = str(session_dir / fname)
        else:
            artifacts[name] = None

    start_dt = datetime.fromtimestamp(started_at, tz=timezone.utc).isoformat()
    end_dt = datetime.fromtimestamp(ended_at, tz=timezone.utc).isoformat()

    return {
        "schema_version": MANIFEST_SCHEMA,
        "session_id": session_id,
        "task_id": task_id,
        "source": source,
        "source_type": source_type,
        "frame_width": frame_width,
        "frame_height": frame_height,
        "fps": fps,
        "detect_every": detect_every,
        "started_at": start_dt,
        "ended_at": end_dt,
        "artifacts": artifacts,
        "label_policy": {
            "type": "machine_generated_weak_labels",
            "is_ground_truth": False,
        },
    }


# ── Validator ──

def _is_json(line: str) -> bool:
    try:
        json.loads(line)
        return True
    except Exception:
        return False


def validate_capture_session(session_dir: str | Path) -> tuple[bool, dict[str, Any]]:
    root = Path(session_dir)
    result: dict[str, Any] = {
        "frames": 0,
        "frames_with_hands": 0,
        "frames_with_detections": 0,
        "machine_labelled_events": 0,
        "missing_or_invalid": 0,
        "raw_video": "absent",
        "errors": [],
    }

    manifest_path = root / "session_manifest.json"
    if not manifest_path.exists():
        result["errors"].append("session_manifest.json missing")
        return False, result
    try:
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("schema_version") != MANIFEST_SCHEMA:
            result["errors"].append(f"manifest schema: {manifest.get('schema_version')} != {MANIFEST_SCHEMA}")
    except Exception as e:
        result["errors"].append(f"manifest parse: {e}")
        return False, result

    # check declared artifacts exist
    for name, path_str in manifest.get("artifacts", {}).items():
        if path_str and not (root / Path(path_str).name).exists():
            result["errors"].append(f"declared artifact missing: {name} ({path_str})")

    # check observations.jsonl
    obs_path = root / "observations.jsonl"
    if not obs_path.exists():
        result["errors"].append("observations.jsonl missing")
        return False, result

    last_frame = -1
    last_pts = -1.0
    raw = obs_path.read_text()
    secrets_check(raw)

    for lineno, line in enumerate(raw.strip().split("\n"), 1):
        if not line.strip():
            continue
        if not _is_json(line):
            result["missing_or_invalid"] += 1
            result["errors"].append(f"L{lineno}: not valid JSON")
            continue
        try:
            obs = json.loads(line)
        except Exception:
            result["missing_or_invalid"] += 1
            continue

        if obs.get("schema_version") != SCHEMA_VERSION:
            result["missing_or_invalid"] += 1
            result["errors"].append(f"L{lineno}: wrong schema_version")
            continue

        fi = obs.get("frame_idx", -1)
        if not isinstance(fi, int) or fi <= last_frame:
            result["missing_or_invalid"] += 1
            result["errors"].append(f"L{lineno}: frame_idx {fi} not monotonic (last={last_frame})")
            continue
        last_frame = fi

        pts = obs.get("pts_ms", -1)
        if pts < last_pts - 0.01:
            result["missing_or_invalid"] += 1
            result["errors"].append(f"L{lineno}: pts_ms {pts} decreased")
        last_pts = max(last_pts, pts)

        result["frames"] += 1

        # hands
        hands = obs.get("hands", [])
        for h in hands:
            lms = h.get("landmarks_norm", [])
            if len(lms) != 21:
                result["missing_or_invalid"] += 1
                result["errors"].append(f"L{lineno}: hand has {len(lms)} landmarks (expected 21)")
            for lm in lms:
                if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in lm):
                    result["missing_or_invalid"] += 1
                    result["errors"].append(f"L{lineno}: non-finite landmark")
                    break
            gc = h.get("grip_closure", 0.0)
            if not math.isfinite(gc):
                result["missing_or_invalid"] += 1
                result["errors"].append(f"L{lineno}: non-finite grip_closure")
        if hands:
            result["frames_with_hands"] += 1
            found_21 = any(len(h.get("landmarks_norm", [])) == 21 for h in hands)
            if not found_21:
                result["missing_or_invalid"] += 1

        # detections
        if obs.get("detections"):
            result["frames_with_detections"] += 1

        # machine labels
        ml = obs.get("machine_labels", {})
        if ml.get("is_ground_truth") is not False:
            result["missing_or_invalid"] += 1
            result["errors"].append(f"L{lineno}: is_ground_truth is not False")
        events = ml.get("emitted_events", [])
        result["machine_labelled_events"] += len(events)

    # raw video check
    raw_vid = root / "raw_video.mp4"
    if raw_vid.exists():
        result["raw_video"] = "present"
        import cv2
        cap = cv2.VideoCapture(str(raw_vid))
        vw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        vh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        vfc = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        cap.release()
        if vw != manifest.get("frame_width", 0) or vh != manifest.get("frame_height", 0):
            result["errors"].append(f"raw video resolution {vw}x{vh} != manifest")
        if abs(vfc - result["frames"]) > 3:
            result["errors"].append(f"raw video frames {vfc} != obs frames {result['frames']}")

    passed = result["missing_or_invalid"] == 0 and len([e for e in result["errors"] if "missing" in e.lower()]) == 0

    return passed, result


def secrets_check(text: str) -> None:
    for kw in ("sk-ws", "DASHSCOPE_API_KEY", "Bearer sk-", "Authorization: Bearer"):
        if kw in text:
            raise ValueError(f"Secrets leak detected in capture data: {kw}")
