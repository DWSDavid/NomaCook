"""Wrap perception / scripted signals into deterministic EventEnvelopes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from perception.fusion import InteractionEvent
from server.events import EventEnvelope, create_event
from server.events.schema import EvidencePayload
from server.perception.tomato_egg_signals import TomatoEggColorSignals

from .session import event_id_for, t_server_for

INTERACTION_TYPE = "perception.hand_object_relation"

# Demo presence rules keyed by tomato-egg SOP step ids. Each entry:
# (payload state string expected by the SOP, canonical labels that must all
# be visible in the same keyframe). Confidence is the weakest member.
TOMATO_EGG_PRESENCE: dict[str, list[tuple[str, frozenset[str]]]] = {
    "step_01_prepare": [
        ("tomato_egg_tools_ready", frozenset({"tomato", "egg", "bowl"}))
    ],
    "step_04_combine_and_plate": [
        ("food_on_plate", frozenset({"plate", "wok"}))
    ],
}


def _base(
    *,
    session_id: str,
    seq: int,
    event_type: str,
    pts_ms: float,
    frame_idx: int | None,
    source: str,
    payload: Any,
    confidence: float | None,
) -> EventEnvelope:
    stamp = t_server_for(pts_ms)
    return create_event(
        session_id=session_id,
        seq=seq,
        event_type=event_type,
        t_device_ms=pts_ms,
        t_server_est=stamp,
        received_at=stamp,
        frame_id=None if frame_idx is None else f"frame_{frame_idx:06d}",
        source=source,
        payload=payload,
        event_id=event_id_for(session_id, seq),
        confidence=confidence,
    )


def interaction_event(
    ev: InteractionEvent, *, session_id: str, seq: int
) -> EventEnvelope:
    relation = "holding" if "holding" in ev.event else "near"
    hand = ev.hand.lower() if ev.hand.lower() in ("left", "right") else "unknown"
    payload = EvidencePayload(
        relation=relation,
        phase="end" if ev.event.endswith("_end") else "start",
        hand=hand,
        object_class=ev.object,
        relation_confidence=ev.conf,
        signals={},
    )
    return _base(
        session_id=session_id,
        seq=seq,
        event_type=INTERACTION_TYPE,
        pts_ms=ev.t * 1000.0,
        frame_idx=ev.frame,
        source="fusion_v1",
        payload=payload,
        confidence=ev.conf,
    )


def presence_states(
    step_id: str, detections: Sequence[Any]
) -> list[tuple[str, float]]:
    best: dict[str, float] = {}
    for det in detections:
        label = det.canonical_label
        best[label] = max(best.get(label, 0.0), float(det.conf))
    states: list[tuple[str, float]] = []
    for state, required in TOMATO_EGG_PRESENCE.get(step_id, []):
        if required <= set(best):
            states.append(
                (state, round(min(best[label] for label in required), 4))
            )
    return sorted(states)


def objects_present_event(
    state: str,
    conf: float,
    *,
    session_id: str,
    seq: int,
    step_id: str,
    pts_ms: float,
    frame_idx: int,
) -> EventEnvelope:
    return _base(
        session_id=session_id,
        seq=seq,
        event_type="perception.objects_present",
        pts_ms=pts_ms,
        frame_idx=frame_idx,
        source="context_presence_v1",
        payload={"step_id": step_id, "state": state},
        confidence=conf,
    )


def roi_color_event(
    signals: TomatoEggColorSignals,
    *,
    session_id: str,
    seq: int,
    step_id: str,
    pts_ms: float,
    frame_idx: int,
) -> EventEnvelope:
    return _base(
        session_id=session_id,
        seq=seq,
        event_type="perception.roi_color",
        pts_ms=pts_ms,
        frame_idx=frame_idx,
        source="opencv_hsv_tomato_egg_v1",
        payload=signals.payload(step_id),
        confidence=signals.confidence,
    )


def load_script(path: str | Path) -> list[dict]:
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("script must be a JSON array")

    indexed_rows: list[dict] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError("every script row must be a JSON object")
        indexed_rows.append({**row, "_index": index})
    return sorted(indexed_rows, key=lambda row: (row["pts_ms"], row["_index"]))


def scripted_event(
    row: dict,
    index: int,
    *,
    session_id: str,
    seq: int,
    question_event_id: str | None,
) -> EventEnvelope:
    pts_ms = float(row["pts_ms"])
    step_id = row["step_id"]
    if row["type"] == "vlm.step_assessment":
        return _base(
            session_id=session_id,
            seq=seq,
            event_type="vlm.step_assessment",
            pts_ms=pts_ms,
            frame_idx=None,
            source="scripted_vlm_v0",
            payload={
                "step_id": step_id,
                "phase": row.get("phase", "likely_complete"),
                "reason": "scripted",
            },
            confidence=float(row.get("confidence", 0.8)),
        )
    if row["type"] == "voice.user_confirmation":
        return _base(
            session_id=session_id,
            seq=seq,
            event_type="voice.user_confirmation",
            pts_ms=pts_ms,
            frame_idx=None,
            source="scripted_voice_v0",
            payload={
                "step_id": step_id,
                "confirmed": True,
                "transcript_event_id": f"script_line_{index}",
                "question_event_id": question_event_id or f"script_q_{index}",
            },
            confidence=float(row.get("confidence", 0.95)),
        )
    raise ValueError(f"unsupported scripted event type {row['type']!r}")
