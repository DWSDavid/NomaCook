from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np

from server.engine import StateEngine, load_recipe
from server.pipeline.session import SESSION_EPOCH
from server.pipeline.vlm_hook import VLMConfirmer
from server.vlm.schema import VLMObservation


class StubClient:
    def __init__(self, phase="likely_complete"):
        self.calls = []
        self.phase = phase

    def analyze_image(self, request, image_bytes, *, mime_type="image/jpeg"):
        self.calls.append(request)
        return VLMObservation(
            decision_id=request.decision_id,
            step_id=request.step_id,
            context_version=request.context_version,
            frame_id=request.frame_id,
            phase=self.phase,
            confidence=0.9,
            reason="stub",
        )


@dataclass
class StubDetection:
    canonical_label: str
    conf: float
    box: tuple[int, int, int, int]
    role: str = "primary"


@dataclass
class StubHand:
    handedness: str
    box: tuple[int, int, int, int]
    palm_center: tuple[float, float]
    is_gripping: bool


def _engine() -> StateEngine:
    return StateEngine(
        session_id="ses_v",
        recipe=load_recipe("sop/tomato_egg.json"),
        started_at=SESSION_EPOCH,
    )


def test_confirmer_calls_once_then_respects_min_gap():
    engine, client = _engine(), StubClient()
    confirmer = VLMConfirmer(client, min_gap_ms=10_000.0)
    frame = np.zeros((60, 80, 3), dtype=np.uint8)
    # score 0 < question_min_score 0.3 -> below band, no call
    assert (
        confirmer.maybe_confirm(
            engine.context,
            engine.current_step,
            frame,
            session_id="ses_v",
            seq=0,
            pts_ms=0.0,
            frame_idx=0,
            force_band=False,
        )
        is None
    )

    env = confirmer.maybe_confirm(
        engine.context,
        engine.current_step,
        frame,
        session_id="ses_v",
        seq=0,
        pts_ms=1000.0,
        frame_idx=30,
        force_band=True,
    )
    assert env is not None
    assert env.type == "vlm.step_assessment"
    assert env.payload["phase"] == "likely_complete"
    assert len(client.calls) == 1

    # 5s later: still inside min gap -> no second call
    assert (
        confirmer.maybe_confirm(
            engine.context,
            engine.current_step,
            frame,
            session_id="ses_v",
            seq=1,
            pts_ms=6000.0,
            frame_idx=180,
            force_band=True,
        )
        is None
    )
    # 11s later -> allowed again
    assert (
        confirmer.maybe_confirm(
            engine.context,
            engine.current_step,
            frame,
            session_id="ses_v",
            seq=1,
            pts_ms=12_000.0,
            frame_idx=360,
            force_band=True,
        )
        is not None
    )


def test_periodic_confirmer_calls_below_uncertainty_band_with_recipe_context():
    engine, client = _engine(), StubClient()
    confirmer = VLMConfirmer(
        client,
        dish_name="番茄炒鸡蛋",
        min_gap_ms=9_000.0,
        periodic=True,
    )
    frame = np.zeros((60, 80, 3), dtype=np.uint8)

    env = confirmer.maybe_confirm(
        engine.context,
        engine.current_step,
        frame,
        session_id="ses_v",
        seq=0,
        pts_ms=0.0,
        frame_idx=0,
        detections=[
            StubDetection("kitchen_knife", 0.90, (28, 28, 36, 34)),
        ],
        hands=[StubHand("Right", (20, 20, 40, 40), (30.0, 30.0), True)],
        frame_wh=(80, 60),
    )

    assert env is not None
    request = client.calls[0]
    assert request.dish_name == "番茄炒鸡蛋"
    assert request.step_instruction == (
        f"{engine.current_step.title}：{engine.current_step.instruction}"
    )
    assert request.failure_modes == engine.current_step.failure_modes
    assert "右手拿着菜刀" in request.detection_context
    assert "菜刀在画面中间" in request.detection_context


def test_periodic_confirmer_stays_at_five_seconds_when_fast_gap_is_disabled():
    engine, client = _engine(), StubClient(phase="in_progress")
    confirmer = VLMConfirmer(
        client,
        min_gap_ms=5_000.0,
        fast_gap_ms=None,
        periodic=True,
    )
    frame = np.zeros((60, 80, 3), dtype=np.uint8)

    first = confirmer.maybe_confirm(
        engine.context,
        engine.current_step,
        frame,
        session_id="ses_v",
        seq=0,
        pts_ms=0.0,
        frame_idx=0,
    )
    assert first is not None
    engine.consume(first)
    assert (
        engine.context.step_progress.score
        >= engine.current_step.completion_policy.question_min_score
    )

    assert confirmer.maybe_confirm(
        engine.context,
        engine.current_step,
        frame,
        session_id="ses_v",
        seq=1,
        pts_ms=4_900.0,
        frame_idx=147,
    ) is None
    assert confirmer.maybe_confirm(
        engine.context,
        engine.current_step,
        frame,
        session_id="ses_v",
        seq=1,
        pts_ms=5_000.0,
        frame_idx=150,
    ) is not None
    assert len(client.calls) == 2


def test_confirmer_uses_global_gap_across_a_step_transition():
    engine, client = _engine(), StubClient()
    confirmer = VLMConfirmer(
        client,
        min_gap_ms=5_000.0,
        fast_gap_ms=3_000.0,
        periodic=True,
    )
    frame = np.zeros((60, 80, 3), dtype=np.uint8)

    completed = confirmer.maybe_confirm(
        engine.context,
        engine.current_step,
        frame,
        session_id="ses_v",
        seq=0,
        pts_ms=0.0,
        frame_idx=0,
    )
    assert completed is not None
    assert engine.consume(completed).transition is not None
    assert engine.context.current_step_id == "step_02_beat_eggs"

    # A per-step clock would call again immediately here. Discovering the new
    # step arms a fresh observation window instead.
    assert confirmer.maybe_confirm(
        engine.context,
        engine.current_step,
        frame,
        session_id="ses_v",
        seq=1,
        pts_ms=1_000.0,
        frame_idx=30,
    ) is None
    assert confirmer.maybe_confirm(
        engine.context,
        engine.current_step,
        frame,
        session_id="ses_v",
        seq=1,
        pts_ms=5_900.0,
        frame_idx=177,
    ) is None
    assert confirmer.maybe_confirm(
        engine.context,
        engine.current_step,
        frame,
        session_id="ses_v",
        seq=1,
        pts_ms=6_000.0,
        frame_idx=180,
    ) is not None
    assert len(client.calls) == 2


def test_confirmer_does_not_call_after_session_completion():
    engine, client = _engine(), StubClient()
    confirmer = VLMConfirmer(client, periodic=True)
    frame = np.zeros((60, 80, 3), dtype=np.uint8)
    completed_context = engine.context.model_copy(update={"step_status": "completed"})

    assert confirmer.maybe_confirm(
        completed_context,
        engine.current_step,
        frame,
        session_id="ses_v",
        seq=0,
        pts_ms=0.0,
        frame_idx=0,
    ) is None
    assert client.calls == []
