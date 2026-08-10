from __future__ import annotations

import pytest
from perception.tomato_to_fridge_events import (
    TomatoToFridgeTracker,
    _point_in_box,
    _box_center,
    canonicalize_detections,
)


# ── geometry helpers ──


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


# ── Fix 1: detect-every cadence regression ──

IN_TABLE_DET = ("tomato", 0.9, (200, 370, 260, 430))


def test_consistent_inference_updates_produce_stable_with_detect_every_3() -> None:
    tracker = TomatoToFridgeTracker(
        frame_width=640, frame_height=480, stability_frames=3,
    )
    all_events: list = []

    for frame_idx in range(15):
        inference = frame_idx % 3 == 0
        if inference:
            all_events.extend(tracker.update(
                t_ms=frame_idx * 33.0,
                detections=[IN_TABLE_DET],
                hands=[],
                interaction_events=[],
            ))

    assert any(e.event_type == "OBJECT_STABLE_IN_REGION" for e in all_events)
    assert not any(e.event_type == "VISIBILITY_LOST" for e in all_events)


def test_skipped_frames_do_not_accumulate_lost_counter() -> None:
    tracker = TomatoToFridgeTracker(
        frame_width=640, frame_height=480, stability_frames=2,
    )
    all_events: list = []

    for _ in range(4):
        all_events.extend(tracker.update(
            t_ms=100, detections=[IN_TABLE_DET], hands=[], interaction_events=[],
        ))
    assert any(e.event_type == "OBJECT_STABLE_IN_REGION" for e in all_events)

    # No further updates at all - skipped frames are just not calling update().
    # Tracker state is frozen; no spurious events.
    lost_after = any(e.event_type == "VISIBILITY_LOST" for e in all_events)
    assert not lost_after


def test_holding_started_from_gripping_hand_over_tomato() -> None:
    tracker = TomatoToFridgeTracker(frame_width=640, frame_height=480)
    all_events = tracker.update(
        t_ms=100,
        detections=[IN_TABLE_DET],
        hands=[],
        interaction_events=[("hand_holding_object", "right", "tomato")],
    )
    assert any(e.event_type == "HOLDING_STARTED" for e in all_events)


# ── Fix 2: canonicalization ──


def test_canonicalize_maps_aliases_to_single_label() -> None:
    dets = [
        ("tomato", 0.9, (10, 10, 50, 50)),
        ("cherry tomato", 0.7, (12, 12, 48, 48)),
        ("red fruit", 0.5, (15, 15, 45, 45)),
        ("refrigerator", 0.8, (100, 100, 200, 200)),
        ("fridge", 0.6, (102, 102, 198, 198)),
    ]
    result = canonicalize_detections(dets)
    labels = {r[0] for r in result}
    assert labels == {"tomato", "refrigerator"}
    assert len(result) == 2


def test_canonicalize_keeps_highest_confidence() -> None:
    dets = [
        ("tomato", 0.6, (1, 1, 10, 10)),
        ("cherry tomato", 0.9, (2, 2, 9, 9)),
    ]
    result = canonicalize_detections(dets)
    assert result[0][0] == "tomato"
    assert result[0][1] == 0.9


def test_canonicalize_drops_unknown_labels() -> None:
    dets = [
        ("table", 0.8, (0, 0, 100, 100)),
        ("tomato", 0.7, (10, 10, 50, 50)),
    ]
    result = canonicalize_detections(dets)
    assert {r[0] for r in result} == {"tomato"}


# ── Fix: empty inference updates are real negative observations ──


def test_empty_inference_updates_accumulate_loss() -> None:
    """After tomato was present, consecutive empty inference frames
    must trigger VISIBILITY_LOST. Skipped (non-inference) frames must not."""
    tracker = TomatoToFridgeTracker(
        frame_width=640, frame_height=480, stability_frames=2,
    )
    det = ("tomato", 0.9, (200, 370, 260, 430))
    all_events: list = []

    # First: establish tomato presence
    for _ in range(3):
        all_events.extend(tracker.update(
            t_ms=100, detections=[det], hands=[], interaction_events=[],
        ))
    # Then: empty inference frames (real negative observations)
    for _ in range(4):
        all_events.extend(tracker.update(
            t_ms=200, detections=[], hands=[], interaction_events=[],
        ))

    assert any(e.event_type == "VISIBILITY_LOST" for e in all_events)


# ── DESTINATION_INTERACTION hardening ──


def test_destination_interaction_fires_per_inference_frame() -> None:
    """Each inference frame where holding + hand-near-fridge fires an event.
    No dedup — engine needs consecutive_hits accumulation."""
    tracker = TomatoToFridgeTracker(
        frame_width=640, frame_height=480, stability_frames=2,
    )
    det = ("tomato", 0.9, (200, 370, 260, 430))
    fridge = ("refrigerator", 0.6, (100, 100, 300, 250))
    palm = (200.0, 200.0)

    # establish tomato + fridge
    for _ in range(3):
        tracker.update(t_ms=100, detections=[det, fridge], hands=[],
                       interaction_events=[])
    # holding + hand near fridge
    all_events: list = []
    for _ in range(3):
        all_events.extend(tracker.update(
            t_ms=200, detections=[det, fridge],
            hands=[("Right", palm, (180, 180, 220, 220), True)],
            interaction_events=[("hand_holding_object", "Right", "tomato")],
        ))

    dest_events = [e for e in all_events if e.event_type == "DESTINATION_INTERACTION"]
    assert len(dest_events) >= 2


def test_single_frame_no_destination_without_holding() -> None:
    """A single inference frame with hand near fridge but no holding
    must NOT produce DESTINATION_INTERACTION."""
    tracker = TomatoToFridgeTracker(
        frame_width=640, frame_height=480, stability_frames=2,
    )
    det = ("tomato", 0.9, (200, 370, 260, 430))
    fridge = ("refrigerator", 0.6, (100, 100, 300, 250))
    palm = (200.0, 200.0)

    for _ in range(3):
        tracker.update(t_ms=100, detections=[det, fridge], hands=[],
                       interaction_events=[])
    all_events = tracker.update(
        t_ms=200, detections=[det, fridge],
        hands=[("Right", palm, (180, 180, 220, 220), False)],
        interaction_events=[],
    )
    dest_events = [e for e in all_events if e.event_type == "DESTINATION_INTERACTION"]
    assert len(dest_events) == 0


def test_origin_not_set_inside_known_fridge() -> None:
    """If fridge is detected first and tomato is inside it,
    _origin_anchor must NOT be set — preventing wrong table learning."""
    tracker = TomatoToFridgeTracker(
        frame_width=640, frame_height=480, stability_frames=3,
    )
    fridge = ("refrigerator", 0.6, (10, 10, 300, 200))
    # tomato inside fridge box
    det = ("tomato", 0.9, (50, 50, 90, 90))

    for _ in range(4):
        tracker.update(t_ms=100, detections=[det, fridge], hands=[],
                       interaction_events=[])

    assert tracker._origin_anchor is None

