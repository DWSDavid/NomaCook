from __future__ import annotations

from datetime import UTC, datetime

import numpy as np

from server.engine import StateEngine, load_recipe
from server.pipeline.session import SESSION_EPOCH
from server.pipeline.vlm_hook import VLMConfirmer
from server.vlm.schema import VLMObservation


class StubClient:
    def __init__(self):
        self.calls = []

    def analyze_image(self, request, image_bytes, *, mime_type="image/jpeg"):
        self.calls.append(request)
        return VLMObservation(
            decision_id=request.decision_id,
            step_id=request.step_id,
            context_version=request.context_version,
            frame_id=request.frame_id,
            phase="likely_complete",
            confidence=0.9,
            reason="stub",
        )


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
