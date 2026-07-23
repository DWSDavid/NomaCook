"""Hand-object interaction signals: pure geometry + debouncing.

Deliberately depends only on numpy and primitives (boxes, points, bools), not
on detector/hands classes, so the logic is unit-testable without loading any
model. This output is Phase-2 state-engine *evidence* (~0.4 weight), not truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field

Box = tuple[int, int, int, int]  # xyxy


# ---------------------------------------------------------------- geometry

def box_area(box: Box) -> float:
    x1, y1, x2, y2 = box
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def intersection_area(a: Box, b: Box) -> float:
    x1 = max(a[0], b[0])
    y1 = max(a[1], b[1])
    x2 = min(a[2], b[2])
    y2 = min(a[3], b[3])
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def containment(inner: Box, outer: Box) -> float:
    """Fraction of `inner`'s area that lies inside `outer`. 0..1."""
    ia = box_area(inner)
    if ia <= 0:
        return 0.0
    return intersection_area(inner, outer) / ia


def point_box_distance(point: tuple[float, float], box: Box) -> float:
    """Euclidean distance from a point to a box (0 if inside)."""
    px, py = point
    x1, y1, x2, y2 = box
    dx = max(x1 - px, 0.0, px - x2)
    dy = max(y1 - py, 0.0, py - y2)
    return (dx * dx + dy * dy) ** 0.5


# ---------------------------------------------------------- classification

# An object is "near" a hand when the palm is within this many hand-widths
# of the object box. Hand-relative, so it survives resolution/zoom changes.
NEAR_FACTOR = 1.0
# "Holding" needs this much of the hand box overlapping the object box.
HOLD_OVERLAP = 0.25


def classify_interaction(
    palm_center: tuple[float, float],
    hand_box: Box,
    is_gripping: bool,
    obj_box: Box,
) -> str | None:
    """Return "holding", "near", or None for one hand/object pair."""
    hand_w = max(hand_box[2] - hand_box[0], 1)
    overlap = containment(hand_box, obj_box)
    palm_dist = point_box_distance(palm_center, obj_box)

    if is_gripping and (overlap >= HOLD_OVERLAP or palm_dist == 0.0):
        return "holding"
    if palm_dist <= NEAR_FACTOR * hand_w:
        return "near"
    return None


# --------------------------------------------------------------- debounce

@dataclass
class InteractionEvent:
    t: float
    frame: int
    event: str  # e.g. "hand_holding_object" / "hand_near_object_end"
    hand: str
    object: str
    conf: float
    hand_box: Box
    obj_box: Box


@dataclass
class _PairState:
    kind: str | None = None  # current debounced state
    candidate: str | None = None
    streak: int = 0
    last_boxes: tuple[Box, Box] | None = None
    last_conf: float = 0.0


_EVENT_NAME = {"near": "hand_near_object", "holding": "hand_holding_object"}


class InteractionTracker:
    """Debounce raw per-frame classifications into start/end events.

    A state change must persist `k_frames` consecutive frames before an event
    fires — detector jitter otherwise floods the log with near/holding flapping.
    """

    def __init__(self, k_frames: int = 3) -> None:
        self.k = k_frames
        self._pairs: dict[tuple[str, str], _PairState] = {}

    def update(
        self,
        t: float,
        frame: int,
        hands: list[tuple[str, tuple[float, float], Box, bool]],
        detections: list[tuple[str, float, Box]],
    ) -> list[InteractionEvent]:
        """hands: (handedness, palm_center, hand_box, is_gripping)
        detections: (label, conf, obj_box). Returns newly fired events."""
        raw: dict[tuple[str, str], tuple[str, float, Box, Box]] = {}
        for handed, palm, hbox, grip in hands:
            for label, conf, obox in detections:
                kind = classify_interaction(palm, hbox, grip, obox)
                if kind is None:
                    continue
                key = (handed, label)
                # A pair can only hold one state; "holding" outranks "near".
                if key not in raw or kind == "holding":
                    raw[key] = (kind, conf, hbox, obox)

        events: list[InteractionEvent] = []
        for key in set(self._pairs) | set(raw):
            st = self._pairs.setdefault(key, _PairState())
            new_kind = raw[key][0] if key in raw else None
            if key in raw:
                _, st.last_conf, hbox, obox = raw[key]
                st.last_boxes = (hbox, obox)

            if new_kind == st.candidate:
                st.streak += 1
            else:
                st.candidate = new_kind
                st.streak = 1

            if st.candidate != st.kind and st.streak >= self.k:
                hand, obj = key
                hbox, obox = st.last_boxes or ((0, 0, 0, 0), (0, 0, 0, 0))
                if st.kind is not None:  # close the old state
                    events.append(
                        InteractionEvent(
                            t, frame, _EVENT_NAME[st.kind] + "_end",
                            hand, obj, st.last_conf, hbox, obox,
                        )
                    )
                if st.candidate is not None:  # open the new one
                    events.append(
                        InteractionEvent(
                            t, frame, _EVENT_NAME[st.candidate],
                            hand, obj, st.last_conf, hbox, obox,
                        )
                    )
                st.kind = st.candidate

            if st.kind is None and st.candidate is None:
                self._pairs.pop(key, None)

        return events
