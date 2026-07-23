from __future__ import annotations

import numpy as np

from perception.hands import HandTracker


def _frame() -> np.ndarray:
    return np.zeros((120, 160, 3), dtype=np.uint8)


def test_external_timestamps_drive_internal_clock():
    tracker = HandTracker()
    try:
        tracker.detect(_frame(), timestamp_ms=0.0)
        tracker.detect(_frame(), timestamp_ms=33.4)
        tracker.detect(_frame(), timestamp_ms=66.7)
        assert tracker.last_timestamp_ms == 67  # ceil + 单调
    finally:
        tracker.close()


def test_non_increasing_timestamp_is_bumped_not_crashed():
    tracker = HandTracker()
    try:
        tracker.detect(_frame(), timestamp_ms=100.0)
        tracker.detect(_frame(), timestamp_ms=100.0)  # 同帧时间重复
        assert tracker.last_timestamp_ms == 101
    finally:
        tracker.close()


def test_default_still_uses_internal_33ms_clock():
    tracker = HandTracker()
    try:
        tracker.detect(_frame())
        tracker.detect(_frame())
        assert tracker.last_timestamp_ms == 66
    finally:
        tracker.close()
