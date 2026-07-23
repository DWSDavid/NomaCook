"""Thin Google GenAI adapter; business validation remains in schema.py."""

from __future__ import annotations

import os

from google import genai
from google.genai import types

from .schema import VLMDecisionRequest, VLMObservation


DEFAULT_VLM_MODEL = "gemini-3.6-flash"

SYSTEM_PROMPT = """
你是 NomaChef 的低频视觉确认器。只根据当前图片判断指定步骤的静态结束状态。
不要根据菜谱常识猜测用户已经做过某个动作；看不清时降低 confidence，并选择
not_started 或 in_progress。风险字段只报告图片中直接可见的风险。reason 保持简短。
必须原样回传 decision_id、step_id、context_version 和 frame_id。
""".strip()


class GeminiVLMClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        key = (api_key or os.getenv("GEMINI_API_KEY") or "").strip()
        if not key:
            raise RuntimeError("GEMINI_API_KEY is required for VLM calls")
        self.model = model or os.getenv("GEMINI_VLM_MODEL", DEFAULT_VLM_MODEL)
        self._client = genai.Client(api_key=key)

    def close(self) -> None:
        self._client.close()

    def analyze_image(
        self,
        request: VLMDecisionRequest,
        image_bytes: bytes,
        *,
        mime_type: str = "image/jpeg",
    ) -> VLMObservation:
        if not image_bytes:
            raise ValueError("image_bytes cannot be empty")
        prompt = (
            f"{SYSTEM_PROMPT}\n\n"
            f"decision_id: {request.decision_id}\n"
            f"step_id: {request.step_id}\n"
            f"context_version: {request.context_version}\n"
            f"frame_id: {request.frame_id}\n"
            f"静态完成条件: {request.completion_check}\n"
            f"相关物体: {', '.join(request.expected_objects) or '未指定'}"
        )
        response = self._client.models.generate_content(
            model=self.model,
            contents=[
                prompt,
                types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=VLMObservation,
            ),
        )
        if not response.text:
            raise RuntimeError("Gemini VLM returned no structured text")
        return VLMObservation.model_validate_json(response.text)
