"""Unit tests for perception/fusion.py — pure geometry, no models loaded."""

from perception.fusion import (
    InteractionTracker,
    classify_interaction,
    containment,
    point_box_distance,
)

HAND = (100, 100, 200, 200)  # 100px-wide hand box
PALM = (150.0, 150.0)


# ---------------------------------------------------------------- geometry

def test_containment_full_partial_none():
    assert containment((0, 0, 10, 10), (0, 0, 20, 20)) == 1.0
    assert containment((0, 0, 10, 10), (5, 0, 20, 10)) == 0.5
    assert containment((0, 0, 10, 10), (50, 50, 60, 60)) == 0.0


def test_point_box_distance():
    assert point_box_distance((5, 5), (0, 0, 10, 10)) == 0.0  # inside
    assert point_box_distance((15, 5), (0, 0, 10, 10)) == 5.0  # right of box
    assert point_box_distance((13, 14), (0, 0, 10, 10)) == 5.0  # 3-4-5 corner


# ---------------------------------------------------------- classification

def test_gripping_overlapping_object_is_holding():
    obj = (120, 120, 260, 260)  # overlaps well over 25% of hand box
    assert classify_interaction(PALM, HAND, True, obj) == "holding"


def test_open_hand_on_object_is_only_near():
    obj = (120, 120, 260, 260)
    assert classify_interaction(PALM, HAND, False, obj) == "near"


def test_gripping_far_object_is_not_holding():
    obj = (500, 500, 600, 600)  # ~4.4 hand-widths away
    assert classify_interaction(PALM, HAND, True, obj) is None


def test_near_threshold_is_hand_relative():
    obj = (240, 100, 400, 200)  # 90px from palm < 1.0 * 100px hand width
    assert classify_interaction(PALM, HAND, False, obj) == "near"
    far = (500, 100, 600, 200)  # 350px away
    assert classify_interaction(PALM, HAND, False, far) is None


# --------------------------------------------------------------- debounce

def _step(tracker, frame, kind_obj_box):
    """One tracker update with a single Right hand and a single bottle."""
    hands = [("Right", PALM, HAND, kind_obj_box[1])]
    dets = [("bottle", 0.6, kind_obj_box[0])]
    return tracker.update(t=frame * 0.1, frame=frame, hands=hands, detections=dets)


def test_event_fires_only_after_k_consecutive_frames():
    tracker = InteractionTracker(k_frames=3)
    on_hand = ((120, 120, 260, 260), True)  # gripping, overlapping
    assert _step(tracker, 0, on_hand) == []
    assert _step(tracker, 1, on_hand) == []
    events = _step(tracker, 2, on_hand)
    assert [e.event for e in events] == ["hand_holding_object"]
    assert events[0].hand == "Right" and events[0].object == "bottle"


def test_single_frame_flicker_is_swallowed():
    tracker = InteractionTracker(k_frames=3)
    on_hand = ((120, 120, 260, 260), True)
    away = ((500, 500, 600, 600), True)
    for f in range(3):
        _step(tracker, f, on_hand)  # holding established at frame 2
    assert _step(tracker, 3, away) == []  # 1-frame dropout: no _end event
    assert _step(tracker, 4, on_hand) == []  # back to holding, no new event
    assert _step(tracker, 5, on_hand) == []


def test_sustained_release_emits_end_event():
    tracker = InteractionTracker(k_frames=3)
    on_hand = ((120, 120, 260, 260), True)
    away = ((500, 500, 600, 600), True)
    for f in range(3):
        _step(tracker, f, on_hand)
    assert _step(tracker, 3, away) == []
    assert _step(tracker, 4, away) == []
    events = _step(tracker, 5, away)
    assert [e.event for e in events] == ["hand_holding_object_end"]


def test_holding_outranks_near_for_same_pair():
    tracker = InteractionTracker(k_frames=1)
    hands = [("Right", PALM, HAND, True)]
    # Same label twice: one box merely near, one overlapping — expect holding.
    dets = [("bottle", 0.5, (290, 100, 400, 200)), ("bottle", 0.6, (120, 120, 260, 260))]
    events = tracker.update(t=0.0, frame=0, hands=hands, detections=dets)
    assert [e.event for e in events] == ["hand_holding_object"]
