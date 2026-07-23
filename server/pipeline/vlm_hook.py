"""Trigger real VLM keyframe confirmation from engine uncertainty."""

from __future__ import annotations

import cv2
import numpy as np

from server.events import EventEnvelope
from server.vlm.schema import VLMDecisionRequest, validate_observation

from .session import t_server_for


class VLMConfirmer:
    def __init__(self, client, *, min_gap_ms: float = 10_000.0) -> None:
        self._client = client
        self._min_gap_ms = min_gap_ms
        self._last_call_ms: dict[str, float] = {}

    def maybe_confirm(
        self,
        context,
        step,
        frame_bgr: np.ndarray,
        *,
        session_id: str,
        seq: int,
        pts_ms: float,
        frame_idx: int,
        force_band: bool = False,
    ) -> EventEnvelope | None:
        in_band = (
            context.pending_question is not None
            or context.step_progress.score
            >= step.completion_policy.question_min_score
        )
        if not force_band and not in_band:
            return None

        last = self._last_call_ms.get(step.id)
        if last is not None and pts_ms - last < self._min_gap_ms:
            return None
        self._last_call_ms[step.id] = pts_ms

        frame_id = f"frame_{frame_idx:06d}"
        requested_at = t_server_for(pts_ms)
        request = VLMDecisionRequest.create(
            decision_id=f"dec_{session_id}_{seq}_vlm",
            session_id=session_id,
            step_id=step.id,
            context_version=context.context_version,
            frame_id=frame_id,
            requested_at=requested_at,
            completion_check=step.completion_check,
            expected_objects=step.objects_involved,
        )
        ok, encoded = cv2.imencode(
            ".jpg", frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, 85]
        )
        if not ok:
            return None
        observation = self._client.analyze_image(request, encoded.tobytes())
        validated = validate_observation(
            request, observation, received_at=requested_at
        )
        return validated.to_event(
            seq=seq,
            t_device_ms=pts_ms,
            source="gemini_vlm_pipeline_v1",
        )
