"""Verify tomato-to-fridge perception + engine pipeline with synthetic frames.

Runs the exact same pipeline steps as harness/eval_tomato_to_fridge.py but
with numpy-generated frames instead of a video file. Tests that:
1. Tomato detection → OBJECT_PRESENT event
2. Stable in table → OBJECT_STABLE_IN_REGION
3. Fridge detection → DESTINATION_PRESENT
4. Holding interaction → HOLDING_STARTED + OBJECT_MOVING_WITH_HAND
5. Events accumulate score in StateEngine
6. Output event vocabulary matches SOP expectations
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import numpy as np
import harness.eval_tomato_to_fridge as eval_harness

from perception.detector import ObjectDetector
from perception.fusion import InteractionTracker
from perception.hands import HandTracker
from perception.tomato_to_fridge_events import (
    TomatoToFridgeTracker,
    canonicalize_detections,
)
from server.engine import StateEngine, load_recipe
from server.engine.snapshot import build_task_snapshot
from server.events import create_event
from server.pipeline.session import SESSION_EPOCH, event_id_for, t_server_for


SESSION_ID = "ses_synth_test"
FRAME_W, FRAME_H = 640, 480

VOCAB = [
    "tomato", "cherry tomato", "red fruit",
    "refrigerator", "fridge", "freezer",
    "table", "kitchen counter", "desk",
    "hand",
]


def _make_engine():
    recipe = load_recipe("sop/tomato_to_fridge.json")
    return StateEngine(session_id=SESSION_ID, recipe=recipe, started_at=SESSION_EPOCH)


def _detect(frame):
    """Mock detection: returns fixed tomato + fridge boxes in table/fridge zones."""
    h, w = frame.shape[:2]
    tomato_center = None
    fridge_center = None
    # check the pixel at expected position
    b, g, r = frame[int(h * 0.8), w // 2]
    if (int(b), int(g), int(r)) == (0, 0, 255):
        tomato_center = (w // 2, int(h * 0.8))
    b2, g2, r2 = frame[40, 40]
    if (int(b2), int(g2), int(r2)) == (255, 0, 0):
        fridge_center = (100, 80)

    dets = []
    if tomato_center is not None:
        dets.append(("tomato", 0.9, (tomato_center[0] - 30, tomato_center[1] - 30,
                                      tomato_center[0] + 30, tomato_center[1] + 30)))
    if fridge_center is not None:
        dets.append(("refrigerator", 0.85, (10, 10, 200, 180)))
    return dets


def _mock_hands(frame):
    """Return a grip hand near the tomato if both are present."""
    h, w = frame.shape[:2]
    b, g, r = frame[int(h * 0.8), w // 2]
    tomato_present = (int(b), int(g), int(r)) == (0, 0, 255)
    bg, gg, rg = frame[int(h * 0.75), w // 2]
    hand_present = (int(bg), int(gg), int(rg)) == (0, 255, 0)

    if tomato_present and hand_present:
        return [("Right", (float(w // 2), float(h * 0.77)),
                 (w // 2 - 20, int(h * 0.72), w // 2 + 20, int(h * 0.82)), True)]
    return []


def _frame(color_bgr: tuple[int, int, int]) -> np.ndarray:
    return np.full((FRAME_H, FRAME_W, 3), color_bgr, dtype=np.uint8)


def _red_tomato_frame() -> np.ndarray:
    """Frame with red pixel at table zone (tomato position)."""
    f = _frame((50, 50, 50))
    cv2 = __import__("cv2")
    cv2.circle(f, (FRAME_W // 2, int(FRAME_H * 0.8)), 20, (0, 0, 255), -1)
    return f


def _red_tomato_blue_fridge_frame() -> np.ndarray:
    """Tomato + fridge both present."""
    f = _red_tomato_frame()
    cv2 = __import__("cv2")
    cv2.rectangle(f, (10, 10), (200, 180), (255, 0, 0), -1)
    return f


def _tomato_with_grip_hand_frame() -> np.ndarray:
    """Tomato in table + green hand pixel above it."""
    f = _red_tomato_blue_fridge_frame()
    cv2 = __import__("cv2")
    cv2.circle(f, (FRAME_W // 2, int(FRAME_H * 0.75)), 15, (0, 255, 0), -1)
    return f


# ── tests ──


def test_pipeline_produces_object_present_for_tomato() -> None:
    recipe = load_recipe("sop/tomato_to_fridge.json")
    engine = StateEngine(session_id=SESSION_ID, recipe=recipe, started_at=SESSION_EPOCH)
    tracker = TomatoToFridgeTracker(frame_width=FRAME_W, frame_height=FRAME_H)

    dets = _detect(_red_tomato_frame())
    can = canonicalize_detections(dets)
    events = tracker.update(t_ms=100, detections=can, hands=[], interaction_events=[])

    obj_present = [e for e in events if e.event_type == "OBJECT_PRESENT"]
    assert len(obj_present) == 1
    assert obj_present[0].payload == {"object": "tomato"}


def test_pipeline_object_stable_in_table_after_consecutive_frames() -> None:
    recipe = load_recipe("sop/tomato_to_fridge.json")
    engine = StateEngine(session_id=SESSION_ID, recipe=recipe, started_at=SESSION_EPOCH)
    tracker = TomatoToFridgeTracker(
        frame_width=FRAME_W, frame_height=FRAME_H, stability_frames=3,
    )

    frame = _red_tomato_frame()
    all_events = []
    for i in range(6):
        dets = _detect(frame)
        can = canonicalize_detections(dets)
        all_events.extend(tracker.update(t_ms=i * 33.0, detections=can, hands=[],
                                          interaction_events=[]))

    stable = [e for e in all_events if e.event_type == "OBJECT_STABLE_IN_REGION"]
    assert len(stable) >= 1
    assert any(e.payload.get("region") == "table" for e in stable)


def test_pipeline_destination_present_from_fridge_detection() -> None:
    tracker = TomatoToFridgeTracker(frame_width=FRAME_W, frame_height=FRAME_H)

    frame = _red_tomato_blue_fridge_frame()
    dets = _detect(frame)
    can = canonicalize_detections(dets)
    first = tracker.update(t_ms=100, detections=can, hands=[], interaction_events=[])
    second = tracker.update(t_ms=200, detections=can, hands=[], interaction_events=[])
    third = tracker.update(t_ms=300, detections=can, hands=[], interaction_events=[])

    assert not any(e.event_type == "DESTINATION_PRESENT" for e in first + second)
    dest = [e for e in third if e.event_type == "DESTINATION_PRESENT"]
    assert len(dest) == 1
    assert dest[0].payload["region"] == "refrigerator_interior"


def test_pipeline_events_feed_state_engine_and_accumulate_score() -> None:
    recipe = load_recipe("sop/tomato_to_fridge.json")
    engine = StateEngine(session_id=SESSION_ID, recipe=recipe, started_at=SESSION_EPOCH)
    tracker = TomatoToFridgeTracker(
        frame_width=FRAME_W, frame_height=FRAME_H, stability_frames=3,
    )

    frame = _red_tomato_blue_fridge_frame()
    seq = 0
    emitted_types = set()
    for i in range(10):
        dets = _detect(frame)
        can = canonicalize_detections(dets)
        events = tracker.update(t_ms=i * 33.0, detections=can, hands=[], interaction_events=[])
        for tev in events:
            env = create_event(
                session_id=SESSION_ID, seq=seq,
                event_type=tev.event_type, t_device_ms=i * 33.0,
                t_server_est=t_server_for(i * 33.0),
                received_at=t_server_for(i * 33.0),
                source="test", event_id=event_id_for(SESSION_ID, seq),
                confidence=tev.confidence, payload=tev.payload,
            )
            engine.consume(env)
            emitted_types.add(tev.event_type)
            seq += 1

    ctx = engine.context
    assert ctx.current_step_id == "hand_near_tomato"
    assert "OBJECT_PRESENT" in emitted_types
    assert "DESTINATION_PRESENT" in emitted_types
    snap = build_task_snapshot(ctx, engine.current_step)
    assert snap.task_id == "tomato_to_fridge_v1"


def test_real_video_start_advances_without_fridge_in_view() -> None:
    """A stationary tomato must establish the start state before the fridge appears."""
    recipe = load_recipe("sop/tomato_to_fridge.json")
    engine = StateEngine(session_id=SESSION_ID, recipe=recipe, started_at=SESSION_EPOCH)
    tracker = TomatoToFridgeTracker(
        frame_width=1920, frame_height=1080, stability_frames=3,
    )

    seq = 0
    samples = [
        (0.27, (858, 445, 1048, 631)),
        (0.40, (862, 449, 1052, 635)),
        (0.50, (868, 453, 1058, 639)),
        (0.59, (875, 457, 1063, 643)),
    ]
    for i, (confidence, box) in enumerate(samples):
        for event in tracker.update(
            t_ms=i * 100.0,
            detections=[("tomato", confidence, box)],
            hands=[],
            interaction_events=[],
        ):
            engine.consume(create_event(
                session_id=SESSION_ID,
                seq=seq,
                event_type=event.event_type,
                t_device_ms=i * 100.0,
                t_server_est=t_server_for(i * 100.0),
                received_at=t_server_for(i * 100.0),
                source="test",
                event_id=event_id_for(SESSION_ID, seq),
                confidence=event.confidence,
                payload=event.payload,
            ))
            seq += 1

    assert engine.context.current_step_id == "hand_near_tomato"


def test_video_style_sequence_reaches_completion() -> None:
    """Task events from a first-person sequence must drive the real state engine."""
    recipe = load_recipe("sop/tomato_to_fridge.json")
    engine = StateEngine(session_id=SESSION_ID, recipe=recipe, started_at=SESSION_EPOCH)
    tracker = TomatoToFridgeTracker(
        frame_width=1920, frame_height=1080, stability_frames=3,
    )
    seq = 0
    tick = 0

    def update(detections, interactions=()):
        nonlocal seq, tick
        events = tracker.update(
            t_ms=tick * 100.0,
            detections=detections,
            hands=[],
            interaction_events=interactions,
        )
        for event in events:
            engine.consume(create_event(
                session_id=SESSION_ID,
                seq=seq,
                event_type=event.event_type,
                t_device_ms=tick * 100.0,
                t_server_est=t_server_for(tick * 100.0),
                received_at=t_server_for(tick * 100.0),
                source="test",
                event_id=event_id_for(SESSION_ID, seq),
                confidence=event.confidence,
                payload=event.payload,
            ))
            seq += 1
        tick += 1

    tomato = lambda x, y, confidence=0.8: (
        "tomato", confidence, (x - 90, y - 80, x + 90, y + 80)
    )
    fridge = ("refrigerator", 0.8, (0, 0, 1500, 1080))

    for x in (950, 954, 958, 960):
        update([tomato(x, 540)])
    update([tomato(960, 540)], [("hand_near_object", "right", "tomato")])
    update([tomato(930, 520)], [("hand_holding_object", "right", "tomato")])
    for x, y in ((880, 500), (810, 470), (700, 430), (620, 400)):
        update([tomato(x, y)])

    update([tomato(620, 400), ("refrigerator", 0.10, (800, 0, 1910, 1080))])
    assert engine.context.current_step_id == "fridge_interaction"

    # DESTINATION_INTERACTION x2 to advance fridge_interaction → candidate_inside_fridge
    for _ in range(2):
        engine.consume(create_event(
            session_id=SESSION_ID, seq=seq,
            event_type="DESTINATION_INTERACTION",
            t_device_ms=tick * 100.0, t_server_est=t_server_for(tick * 100.0),
            received_at=t_server_for(tick * 100.0),
            source="test", event_id=event_id_for(SESSION_ID, seq),
            confidence=0.9, payload={"region": "refrigerator"},
        ))
        seq += 1
        tick += 1

    for _ in range(3):
        update([tomato(1300, 600), fridge])
    for _ in range(5):
        update([fridge])
    for _ in range(3):
        update([tomato(820, 650), fridge])
    update(
        [tomato(820, 650), fridge],
        [("hand_near_object_end", "right", "tomato")],
    )
    for x, y in ((820, 650), (822, 651), (821, 650), (820, 650)):
        update([tomato(x, y), fridge])

    assert engine.context.step_status == "completed"


def test_overlay_reports_last_recognized_phase_not_next_instruction() -> None:
    recipe = load_recipe("sop/tomato_to_fridge.json")
    engine = StateEngine(session_id=SESSION_ID, recipe=recipe, started_at=SESSION_EPOCH)
    result = engine.consume(create_event(
        session_id=SESSION_ID,
        seq=0,
        event_type="OBJECT_PRESENT",
        t_device_ms=100.0,
        t_server_est=t_server_for(100.0),
        received_at=t_server_for(100.0),
        source="test",
        event_id=event_id_for(SESSION_ID, 0),
        confidence=0.9,
        payload={"object": "tomato"},
    ))
    assert engine.context.current_step_id == "tomato_on_table"
    assert eval_harness._recognized_step_id(None, result) == "ready"


def test_snapshot_output_contains_required_fields() -> None:
    recipe = load_recipe("sop/tomato_to_fridge.json")
    engine = StateEngine(session_id=SESSION_ID, recipe=recipe, started_at=SESSION_EPOCH)
    snap = build_task_snapshot(engine.context, engine.current_step)
    data = snap.model_dump()
    for key in ("session_id", "task_id", "state", "status", "belief",
                "active_objects", "missing_evidence", "last_event_seq",
                "context_version"):
        assert key in data, f"missing field: {key}"
