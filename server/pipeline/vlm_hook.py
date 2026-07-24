"""Trigger real VLM keyframe confirmation from engine uncertainty."""

from __future__ import annotations

import cv2
import numpy as np

from server.events import EventEnvelope
from server.vlm.detection_context import format_scene_context
from server.vlm.schema import VLMDecisionRequest, validate_observation

from .session import t_server_for


class VLMConfirmer:
    def __init__(
        self,
        client,
        *,
        dish_name: str = "",
        min_gap_ms: float = 5_000.0,
        fast_gap_ms: float | None = 3_000.0,
        periodic: bool = False,
    ) -> None:
        if min_gap_ms <= 0:
            raise ValueError("min_gap_ms must be positive")
        if fast_gap_ms is not None and not 0 < fast_gap_ms <= min_gap_ms:
            raise ValueError("fast_gap_ms must be positive and <= min_gap_ms")
        self._client = client
        self._dish_name = dish_name
        self._min_gap_ms = min_gap_ms
        self._fast_gap_ms = fast_gap_ms
        self._periodic = periodic
        # One global clock prevents a step transition from triggering another
        # network request on the immediately following video frame.
        self._last_call_ms: float | None = None
        self._last_step_id: str | None = None

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            close()

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
        detections=None,
        hands=None,
        frame_wh=None,
    ) -> EventEnvelope | None:
        if context.step_status == "completed":
            return None

        if self._last_step_id is None:
            self._last_step_id = step.id
        elif step.id != self._last_step_id:
            # A transition produced by local/scripted evidence may happen long
            # after the prior VLM call. Arm a fresh observation window
            # instead of checking the new step in that same frame.
            self._last_step_id = step.id
            self._last_call_ms = pts_ms
            return None

        in_band = (
            context.pending_question is not None
            or context.step_progress.score
            >= step.completion_policy.question_min_score
        )
        if not force_band and not in_band and not self._periodic:
            return None

        gap_ms = (
            self._fast_gap_ms
            if in_band and self._fast_gap_ms is not None
            else self._min_gap_ms
        )
        last = self._last_call_ms
        # Frame timestamps are floating-point; allow a 1 ms tolerance so a
        # configured 5 s cadence does not silently slip to a later frame.
        if last is not None and pts_ms - last < gap_ms - 1.0:
            return None
        self._last_call_ms = pts_ms

        frame_id = f"frame_{frame_idx:06d}"
        requested_at = t_server_for(pts_ms)
        detection_context = ""
        if detections is not None and frame_wh is not None:
            detection_context = format_scene_context(
                detections,
                hands or [],
                frame_wh,
                step.objects_involved,
            )
        request = VLMDecisionRequest.create(
            decision_id=f"dec_{session_id}_{seq}_vlm",
            session_id=session_id,
            step_id=step.id,
            context_version=context.context_version,
            frame_id=frame_id,
            requested_at=requested_at,
            completion_check=step.completion_check,
            expected_objects=step.objects_involved,
            dish_name=self._dish_name,
            step_instruction=(
                f"{step.title}：{step.instruction}" if step.title
                else step.instruction
            ),
            failure_modes=step.failure_modes,
            detection_context=detection_context,
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
            source="gemini_vlm_pipeline_v2",
        )
