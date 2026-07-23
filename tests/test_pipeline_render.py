from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from server.pipeline.render import AnnotatedVideoWriter, draw_overlay


@dataclass(frozen=True)
class FakeDet:
    canonical_label: str
    conf: float
    box: tuple[int, int, int, int]
    role: str


def test_draw_overlay_mutates_frame_without_crashing_on_chinese():
    frame = np.zeros((240, 320, 3), dtype=np.uint8)
    before = frame.copy()
    draw_overlay(
        frame,
        detections=[FakeDet("wok", 0.83, (10, 40, 200, 200), "primary")],
        step_id="step_02_scramble_egg",
        instruction="热锅加油，倒入蛋液，翻炒至凝固。",
        score=0.4, threshold=0.7,
        pending_question="鸡蛋已经凝固成块并盛出来了吗？",
        recent_events=["hand_holding_object Right/bowl"],
        color_text="color=yellow_dominant",
    )
    assert (frame != before).any()


def test_writer_writes_frames_and_reports_count(tmp_path: Path):
    out = tmp_path / "annotated.mp4"
    writer = AnnotatedVideoWriter(out, fps=30.0, frame_size=(320, 240))
    for _ in range(9):
        writer.write(np.zeros((240, 320, 3), dtype=np.uint8))
    writer.close()
    assert writer.frames_written == 9
    probe = cv2.VideoCapture(str(out))
    assert int(probe.get(cv2.CAP_PROP_FRAME_COUNT)) == 9
    probe.release()


def test_draw_overlay_renders_hand_skeleton():
    import numpy as np
    from dataclasses import dataclass, field

    @dataclass(frozen=True)
    class FakeHand:
        landmarks_px: np.ndarray = field(
            default_factory=lambda: np.array(
                [[60.0 + 4 * i, 120.0 + 3 * i] for i in range(21)]))
        handedness: str = "Right"
        grip_closure: float = 0.61
        is_gripping: bool = True

    frame = np.zeros((240, 320, 3), dtype=np.uint8)
    before = frame.copy()
    draw_overlay(
        frame, detections=[], step_id="step_01_prepare", instruction="x",
        score=0.0, threshold=0.7, pending_question=None,
        recent_events=[], color_text=None, hands=[FakeHand()],
    )
    assert (frame != before).any()


def test_draw_overlay_without_hands_still_works():
    import numpy as np
    frame = np.zeros((240, 320, 3), dtype=np.uint8)
    draw_overlay(
        frame, detections=[], step_id="s", instruction="x", score=0.0,
        threshold=0.7, pending_question=None, recent_events=[], color_text=None,
    )
