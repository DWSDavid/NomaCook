from __future__ import annotations

import pytest
from perception.tomato_to_fridge_events import TomatoToFridgeTracker, _point_in_box, _box_center


def test_point_in_box() -> None:
    box = (10, 20, 100, 80)
    assert _point_in_box((50, 50), box) is True
    assert _point_in_box((5, 50), box) is False
    assert _point_in_box((50, 90), box) is False
    assert _point_in_box((10, 20), box) is True


def test_box_center() -> None:
    assert _box_center((0, 0, 100, 100)) == (50.0, 50.0)


def test_initial_state_no_events() -> None:
    tracker = TomatoToFridgeTracker(frame_width=640, frame_height=480)
    events = tracker.update(
        t_ms=100,
        detections=[],
        hands=[],
        interaction_events=[],
    )
    assert events == []


def test_tomato_present_emits_event() -> None:
    tracker = TomatoToFridgeTracker(frame_width=640, frame_height=480)
    events = tracker.update(
        t_ms=100,
        detections=[("tomato", 0.9, (200, 200, 260, 260))],
        hands=[],
        interaction_events=[],
    )
    assert any(e.event_type == "OBJECT_PRESENT" for e in events)


def test_tomato_in_table_fires_stable_after_stability_frames() -> None:
    tracker = TomatoToFridgeTracker(
        frame_width=640, frame_height=480, stability_frames=3,
    )
    det = ("tomato", 0.9, (200, 370, 260, 430))
    all_events = []
    for _ in range(5):
        all_events.extend(tracker.update(
            t_ms=100, detections=[det], hands=[], interaction_events=[]
        ))
    assert any(e.event_type == "OBJECT_STABLE_IN_REGION" for e in all_events)


def test_tomato_leaves_table_fires_left_region() -> None:
    tracker = TomatoToFridgeTracker(
        frame_width=640, frame_height=480, stability_frames=2,
    )
    in_table = ("tomato", 0.9, (200, 370, 260, 430))
    off_table = ("tomato", 0.9, (200, 100, 260, 160))
    for _ in range(3):
        tracker.update(t_ms=100, detections=[in_table], hands=[], interaction_events=[])
    all_events = []
    for _ in range(6):
        all_events.extend(tracker.update(
            t_ms=100, detections=[off_table], hands=[], interaction_events=[]
        ))
    assert any(e.event_type == "OBJECT_LEFT_REGION" for e in all_events)


def test_interaction_holding_emits_holding_started() -> None:
    tracker = TomatoToFridgeTracker(frame_width=640, frame_height=480)
    all_events = tracker.update(
        t_ms=100,
        detections=[("tomato", 0.9, (200, 200, 260, 260))],
        hands=[],
        interaction_events=[("hand_holding_object", "right", "tomato")],
    )
    assert any(e.event_type == "HOLDING_STARTED" for e in all_events)


def test_reset_region_events_allows_re_fire() -> None:
    tracker = TomatoToFridgeTracker(
        frame_width=640, frame_height=480, stability_frames=2,
    )
    det = ("tomato", 0.9, (200, 370, 260, 430))
    for _ in range(3):
        tracker.update(t_ms=100, detections=[det], hands=[], interaction_events=[])
    tracker.reset_region_events()
    all_events = []
    for _ in range(4):
        all_events.extend(tracker.update(
            t_ms=100, detections=[det], hands=[], interaction_events=[]
        ))
    assert any(e.event_type == "OBJECT_STABLE_IN_REGION" for e in all_events)
