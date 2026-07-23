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
