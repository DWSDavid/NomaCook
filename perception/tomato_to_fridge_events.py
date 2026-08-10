"""Tomato-to-fridge task-specific perception → SOP evidence event bridge.

No training. Pure geometry + temporal tracking consuming raw YOLO + MediaPipe
signals already produced by the existing perception layer. Maps object positions
relative to table/fridge regions into the event vocabulary defined in
sop/tomato_to_fridge.json.

Regions:
  - TABLE: learned from the tomato's stable starting position
  - FRIDGE_INTERIOR: refrigerator detected consistently across frames

Events produced (mapped to SOP evidence_rules event_type):
  OBJECT_PRESENT          — YOLO detects tomato
  DESTINATION_PRESENT     — fridge region is known (detected or fallback)
  OBJECT_STABLE_IN_REGION — tomato center in region for K consecutive frames
  HAND_NEAR_STARTED       — from InteractionTracker (hand_near_object on tomato)
  HAND_NEAR_ENDED         — from InteractionTracker (hand_near_object_end)
  HOLDING_STARTED         — from InteractionTracker (hand_holding_object on tomato)
  HOLDING_ENDED           — from InteractionTracker (hand_holding_object_end)
  OBJECT_MOVING_WITH_HAND — tomato center displacement + hand near/over
  OBJECT_LEFT_REGION      — tomato was in region, now outside N consecutive frames
  OBJECT_ENTERED_REGION   — tomato center enters region
  DESTINATION_INTERACTION — hand near fridge + moving toward it
  VISIBILITY_LOST         — YOLO can't detect tomato anymore
  OBJECT_RETURNED_TO_REGION — tomato returns to a region it previously left
  OBJECT_EXITED_REGION    — tomato leaves fridge interior
  OBJECT_MOVED_AWAY_FROM_DESTINATION — tomato moved away from fridge
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Sequence

Box = tuple[int, int, int, int]
Point = tuple[float, float]

FRIDGE_FALLBACK_FRACTION = 0.30
TABLE_FRACTION = 0.70
STABILITY_FRAMES = 3
TRANSITION_FRAMES = 2
MIN_DISPLACEMENT_PX = 15.0
MIN_SHARED_MOTION_FRAMES = 2
MIN_FRIDGE_CONFIDENCE = 0.35

# ── label canonicalization ──
CANONICAL_LABELS: dict[str, str] = {
    "tomato": "tomato",
    "cherry tomato": "tomato",
    "red fruit": "tomato",
    "refrigerator": "refrigerator",
    "fridge": "refrigerator",
    "freezer": "refrigerator",
}


def canonicalize_detections(
    detections: Sequence[tuple[str, float, Box]],
) -> list[tuple[str, float, Box]]:
    """Dedup overlapping aliases, keep highest-confidence per canonical label."""
    best: dict[str, tuple[float, Box]] = {}
    for label, conf, box in detections:
        canonical = CANONICAL_LABELS.get(label)
        if canonical is None:
            continue
        if canonical not in best or conf > best[canonical][0]:
            best[canonical] = (conf, box)
    return [(label, conf, box) for label, (conf, box) in best.items()]


def _box_center(box: Box) -> Point:
    x1, y1, x2, y2 = box
    return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)


def _point_in_box(pt: Point, box: Box) -> bool:
    px, py = pt
    x1, y1, x2, y2 = box
    return x1 <= px <= x2 and y1 <= py <= y2


def _distance(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


@dataclass
class TomatoToFridgeEvent:
    t_ms: float
    event_type: str
    payload: dict = field(default_factory=dict)
    confidence: float = 1.0


class TomatoToFridgeTracker:
    """Consume per-frame detections + hands, produce SOP evidence events."""

    def __init__(
        self,
        *,
        frame_width: int = 640,
        frame_height: int = 480,
        table_fraction: float = TABLE_FRACTION,
        stability_frames: int = STABILITY_FRAMES,
    ) -> None:
        self._w = frame_width
        self._h = frame_height
        self._table_y0 = int(table_fraction * frame_height)
        self._fridge_box: Box | None = None
        self._fridge_detect_counter: int = 0
        self._fridge_fallback: Box = (
            0, 0, int(frame_width * FRIDGE_FALLBACK_FRACTION), int(frame_height * 0.5)
        )
        self._stability = stability_frames

        self._tomato_history: deque[tuple[Point, float]] = deque(maxlen=16)
        self._origin_anchor: Point | None = None
        self._origin_radius = max(60.0, min(frame_width, frame_height) * 0.10)
        self._stationary_radius = max(15.0, min(frame_width, frame_height) * 0.025)

        self._in_table: bool = False
        self._in_fridge: bool = False
        self._table_exit_counter: int = 0
        self._fridge_entry_counter: int = 0
        self._fridge_exit_counter: int = 0
        self._table_reentry_counter: int = 0
        self._moved_away_counter: int = 0
        self._stable_table_counter: int = 0
        self._stable_fridge_counter: int = 0
        self._shared_motion_counter: int = 0
        self._holding_active: bool = False
        self._fridge_announced: bool = False
        self._tomato_lost_counter: int = 0
        self._tomato_missing_after_fridge: bool = False
        self._release_observed: bool = False

        self._seen_events: set[str] = set()

    @property
    def table_region(self) -> Box:
        return (0, self._table_y0, self._w, self._h)

    @property
    def fridge_region(self) -> Box:
        return self._fridge_box or (0, 0, 0, 0)

    def update(
        self,
        t_ms: float,
        detections: Sequence[tuple[str, float, Box]],
        hands: Sequence[tuple[str, Point, Box, bool]],
        interaction_events: Sequence[tuple[str, str, str]],
    ) -> list[TomatoToFridgeEvent]:
        """ingest per-frame perception, return newly fired SOP events.

        detections: (canonical_label, confidence, box_xyxy)
        hands: (handedness, palm_center, hand_box, is_gripping)
        interaction_events: list of (event_name, hand_label, object_label)
            from InteractionTracker, e.g. ("hand_near_object", "right", "tomato")
        """
        events: list[TomatoToFridgeEvent] = []

        # ── update fridge region ──
        fridge_det = None
        fridge_conf = 0.0
        for label, conf, box in detections:
            if label == "refrigerator":
                fridge_det = box
                fridge_conf = conf
                break
        if fridge_det is not None and fridge_conf >= MIN_FRIDGE_CONFIDENCE:
            self._fridge_detect_counter += 1
            if self._fridge_detect_counter >= self._stability:
                self._fridge_box = fridge_det
            if self._fridge_box is not None and not self._fridge_announced:
                events.append(TomatoToFridgeEvent(
                    t_ms, "DESTINATION_PRESENT",
                    {"region": "refrigerator_interior"}, confidence=fridge_conf,
                ))
                self._fridge_announced = True
        elif self._fridge_box is None:
            self._fridge_detect_counter = 0

        # ── update tomato position ──
        tomato_pos: Point | None = None
        tomato_conf: float = 0.0
        for label, conf, box in detections:
            if label == "tomato":
                tomato_pos = _box_center(box)
                tomato_conf = conf
                break

        if tomato_pos is not None:
            self._tomato_history.append((tomato_pos, tomato_conf))
            self._tomato_lost_counter = 0
        else:
            self._tomato_lost_counter += 1
            if self._fridge_box is not None and self._tomato_lost_counter >= self._stability:
                self._tomato_missing_after_fridge = True
            if self._tomato_lost_counter >= self._stability and not self._has_fired("VISIBILITY_LOST"):
                events.append(TomatoToFridgeEvent(
                    t_ms, "VISIBILITY_LOST",
                    {"object": "tomato"}, confidence=0.7,
                ))
                self._mark_fired("VISIBILITY_LOST")
        if tomato_pos is not None and self._tomato_lost_counter > 0:
            self._tomato_lost_counter = 0

        # ── object present event ──
        if tomato_pos is not None:
            events.append(TomatoToFridgeEvent(
                t_ms, "OBJECT_PRESENT",
                {"object": "tomato"}, confidence=tomato_conf,
            ))

        # ── region checks ──
        fridge_box = self.fridge_region
        if self._origin_anchor is None and len(self._tomato_history) >= self._stability:
            recent = [item[0] for item in list(self._tomato_history)[-self._stability:]]
            if max(_distance(recent[0], point) for point in recent[1:]) <= self._stationary_radius:
                candidate = (
                    sum(point[0] for point in recent) / len(recent),
                    sum(point[1] for point in recent) / len(recent),
                )
                if self._fridge_box is None or not _point_in_box(candidate, self._fridge_box):
                    self._origin_anchor = candidate
                    self._stable_table_counter = self._stability - 1
        in_table_now = (
            tomato_pos is not None
            and self._origin_anchor is not None
            and _distance(tomato_pos, self._origin_anchor) <= self._origin_radius
        )
        in_fridge_now = (
            tomato_pos is not None
            and self._fridge_box is not None
            and self._tomato_missing_after_fridge
            and _point_in_box(tomato_pos, fridge_box)
        )

        # ── OBJECT_STABLE_IN_REGION ──
        if in_table_now:
            self._stable_table_counter += 1
            if self._stable_table_counter >= self._stability:
                self._in_table = True
                self._table_exit_counter = 0
                if not self._has_fired("STABLE_table"):
                    events.append(TomatoToFridgeEvent(
                        t_ms, "OBJECT_STABLE_IN_REGION",
                        {"object": "tomato", "region": "table"}, confidence=tomato_conf,
                    ))
                    self._mark_fired("STABLE_table")
                    self._stable_table_counter = 0
        else:
            self._stable_table_counter = max(0, self._stable_table_counter - 1)

        if in_fridge_now and self._in_fridge and self._release_observed:
            self._stable_fridge_counter += 1
            if self._stable_fridge_counter >= self._stability:
                self._in_fridge = True
                self._fridge_exit_counter = 0
                if not self._has_fired("STABLE_fridge"):
                    events.append(TomatoToFridgeEvent(
                        t_ms, "OBJECT_STABLE_IN_REGION",
                        {"object": "tomato", "region": "refrigerator_interior"},
                        confidence=tomato_conf,
                    ))
                    self._mark_fired("STABLE_fridge")
                    self._stable_fridge_counter = 0
        else:
            self._stable_fridge_counter = max(0, self._stable_fridge_counter - 1)

        # ── OBJECT_LEFT_REGION (table) ──
        if self._in_table and tomato_pos is not None and not in_table_now:
            self._table_exit_counter += 1
            if self._table_exit_counter >= TRANSITION_FRAMES and not self._has_fired("LEFT_table"):
                events.append(TomatoToFridgeEvent(
                    t_ms, "OBJECT_LEFT_REGION",
                    {"object": "tomato", "region": "table"}, confidence=tomato_conf,
                ))
                self._mark_fired("LEFT_table")
                self._in_table = False
                self._table_reentry_counter = 0
        elif in_table_now:
            self._table_exit_counter = 0

        # ── OBJECT_ENTERED_REGION (fridge) ──
        if not self._in_fridge and in_fridge_now:
            self._fridge_entry_counter += 1
            if self._fridge_entry_counter >= TRANSITION_FRAMES and not self._has_fired("ENTERED_fridge"):
                events.append(TomatoToFridgeEvent(
                    t_ms, "OBJECT_ENTERED_REGION",
                    {"object": "tomato", "region": "refrigerator_interior"},
                    confidence=tomato_conf,
                ))
                self._mark_fired("ENTERED_fridge")
                self._in_fridge = True
                self._fridge_entry_counter = 0
        elif not in_fridge_now:
            self._fridge_entry_counter = 0

        # ── OBJECT_EXITED_REGION (fridge) ──
        if self._in_fridge and tomato_pos is not None and not in_fridge_now:
            self._fridge_exit_counter += 1
            if self._fridge_exit_counter >= TRANSITION_FRAMES and not self._has_fired("EXITED_fridge"):
                events.append(TomatoToFridgeEvent(
                    t_ms, "OBJECT_EXITED_REGION",
                    {"object": "tomato", "region": "refrigerator_interior"},
                    confidence=tomato_conf,
                ))
                self._mark_fired("EXITED_fridge")
                self._in_fridge = False
        elif in_fridge_now:
            self._fridge_exit_counter = 0

        # ── OBJECT_RETURNED_TO_REGION (table reentry) ──
        if not self._in_table and in_table_now and self._has_fired("LEFT_table"):
            self._table_reentry_counter += 1
            if self._table_reentry_counter >= TRANSITION_FRAMES:
                events.append(TomatoToFridgeEvent(
                    t_ms, "OBJECT_RETURNED_TO_REGION",
                    {"object": "tomato", "region": "table"}, confidence=tomato_conf,
                ))
                self._in_table = True
                self._table_reentry_counter = 0
                # Reset region-related fired flags so re-events can fire again
                self._unmark_prefix("LEFT_table")
                self._unmark_prefix("ENTERED_fridge")
                self._unmark_prefix("STABLE_fridge")
                self._unmark_prefix("EXITED_fridge")
        elif in_table_now:
            self._table_reentry_counter = 0

        # ── interaction event mapping ──
        for ev_name, hand_label, obj_label in interaction_events:
            if obj_label != "tomato":
                continue

            if ev_name == "hand_near_object":
                events.append(TomatoToFridgeEvent(
                    t_ms, "HAND_NEAR_STARTED", {"object": "tomato"},
                ))
            elif ev_name == "hand_near_object_end":
                events.append(TomatoToFridgeEvent(
                    t_ms, "HAND_NEAR_ENDED", {},
                ))
                if self._in_fridge:
                    self._release_observed = True
                    events.append(TomatoToFridgeEvent(
                        t_ms, "HOLDING_ENDED", {"object": "tomato"},
                    ))
            elif ev_name == "hand_holding_object":
                self._holding_active = True
                events.append(TomatoToFridgeEvent(
                    t_ms, "HOLDING_STARTED", {"object": "tomato"},
                ))
                self._unmark_prefix("VISIBILITY_LOST")
            elif ev_name == "hand_holding_object_end":
                if self._holding_active:
                    events.append(TomatoToFridgeEvent(
                        t_ms, "HOLDING_ENDED", {"object": "tomato"},
                    ))
                if self._in_fridge:
                    self._release_observed = True
                self._holding_active = False

        # ── DESTINATION_INTERACTION ──
        if self._holding_active and tomato_pos is not None:
            hand_near_fridge = False
            for _, palm, _, _ in hands:
                if _point_in_box(palm, fridge_box):
                    hand_near_fridge = True
                    break
            if hand_near_fridge:
                events.append(TomatoToFridgeEvent(
                    t_ms, "DESTINATION_INTERACTION", {"region": "refrigerator"},
                ))

        # ── OBJECT_MOVING_WITH_HAND ──
        if self._holding_active and len(self._tomato_history) >= 3:
            recent = list(self._tomato_history)[-3:]
            p1, p2 = recent[0][0], recent[-1][0]
            disp = _distance(p1, p2)
            if disp >= MIN_DISPLACEMENT_PX:
                self._shared_motion_counter += 1
                if self._shared_motion_counter >= MIN_SHARED_MOTION_FRAMES:
                    events.append(TomatoToFridgeEvent(
                        t_ms, "OBJECT_MOVING_WITH_HAND", {"object": "tomato"},
                    ))
                    self._shared_motion_counter = 0
            else:
                self._shared_motion_counter = max(0, self._shared_motion_counter - 1)

        # ── OBJECT_MOVED_AWAY_FROM_DESTINATION ──
        if self._in_fridge and tomato_pos is not None and not in_fridge_now:
            self._moved_away_counter += 1
            if self._moved_away_counter >= TRANSITION_FRAMES and not self._has_fired("MOVED_AWAY"):
                events.append(TomatoToFridgeEvent(
                    t_ms, "OBJECT_MOVED_AWAY_FROM_DESTINATION", {},
                ))
                self._mark_fired("MOVED_AWAY")
                self._in_fridge = False
        elif in_fridge_now:
            self._moved_away_counter = 0

        return events

    def reset_region_events(self) -> None:
        """Call on step transition so region events can re-fire."""
        keys = list(self._seen_events)
        for k in keys:
            if any(k.startswith(p) for p in (
                "STABLE_", "LEFT_", "ENTERED_", "EXITED_",
                "MOVED_AWAY", "OBJECT_PRESENT_",
                "VISIBILITY_LOST",
            )):
                self._seen_events.discard(k)
        self._fridge_announced = False
        self._table_exit_counter = 0
        self._fridge_entry_counter = 0
        self._fridge_exit_counter = 0
        self._table_reentry_counter = 0
        self._moved_away_counter = 0

    def _has_fired(self, key: str) -> bool:
        return key in self._seen_events

    def _mark_fired(self, key: str) -> None:
        self._seen_events.add(key)

    def _unmark_prefix(self, prefix: str) -> None:
        for k in list(self._seen_events):
            if k.startswith(prefix):
                self._seen_events.discard(k)
