"""Thin Google GenAI adapter; business validation remains in schema.py."""

from __future__ import annotations

import time

from google import genai
from google.genai import types

from server.gemini_config import gemini_api_key, gemini_setting

from .schema import VLMDecisionRequest, VLMObservation


DEFAULT_VLM_MODEL = "gemini-3.6-flash"

# Explicit API-native schema. Passing the pydantic model directly emits
# additionalProperties (from extra="forbid"), which the Gemini API rejects
# with 400 INVALID_ARGUMENT. Field set mirrors VLMObservation; optional
# fields with defaults stay out of `required` so validation still fills them.
_RESPONSE_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "decision_id": types.Schema(type=types.Type.STRING),
        "step_id": types.Schema(type=types.Type.STRING),
        "context_version": types.Schema(type=types.Type.INTEGER),
        "frame_id": types.Schema(type=types.Type.STRING),
        "phase": types.Schema(
            type=types.Type.STRING,
            enum=["not_started", "in_progress", "likely_complete"],
        ),
        "confidence": types.Schema(type=types.Type.NUMBER),
        "observed_objects": types.Schema(
            type=types.Type.ARRAY, items=types.Schema(type=types.Type.STRING)
        ),
        "risk_level": types.Schema(
            type=types.Type.STRING, enum=["none", "warning", "critical"]
        ),
        "risk_reason": types.Schema(type=types.Type.STRING, nullable=True),
        "coach_comment": types.Schema(type=types.Type.STRING, nullable=True),
        "reason": types.Schema(type=types.Type.STRING),
    },
    required=[
        "decision_id", "step_id", "context_version", "frame_id",
        "phase", "confidence", "reason",
    ],
)

SYSTEM_PROMPT = """
你是 NomaChef 状态引擎的低频视觉判别器，不是聊天助手，也不负责讲解下一步。
只根据“当前这一张图片”判断当前步骤的状态；菜名、操作说明和物体清单只是识别上下文，
不能当作步骤已经完成的证据。

判别顺序：
1. 先确认完成条件要求的结果是否直接可见；只做了一部分时选 in_progress。
2. 只有完成条件的关键结果都清楚可见时才选 likely_complete。模糊、遮挡或有歧义时降低
   confidence，宁可选 not_started / in_progress，也不要根据菜谱常识补全动作。
   如果且仅如果“静态完成条件”明确允许用已经进入后续画面来确认剪辑越过当前步骤，
   才能把直接可见的后续阶段作为完成依据；不要自行推断未拍到的动作。
3. 区分锅和碗时不要只看圆形轮廓：灶台上的大号深色金属容器、有长柄或正在受热的，
   优先识别为 wok；离开灶台、较小、装蛋液或备料的容器优先识别为 bowl。
4. observed_objects 使用当前菜品与步骤中的具体形态，例如 chopped tomato、
   beaten egg、scrambled egg、wok、bowl；不要把食物状态写成已完成结论。
5. 风险字段只报告图片中直接可见的风险。reason 用一句简短中文说明可见依据。
6. 每个步骤至少值得主动说一句 coach_comment：当前画面能给出具体、可操作、与当前
   步骤直接相关的帮助（火候、安全或手边动作）时，写一句简短口语化中文；没有新信息
   时才设为 null。不要复述 reason 或步骤指令，不要说“步骤完成了”，也不要预告下一步。
7. 你会额外收到一段“本地检测器”的粗略识别结果作为线索。它可能有重复、误标或漏检，
   只能当提示，绝不能当结论；一切以你在图片里实际看到的为准，冲突时相信画面。

必须原样回传 decision_id、step_id、context_version 和 frame_id。不要输出下一步建议。
""".strip()


class GeminiVLMClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        attempts: int = 3,
    ) -> None:
        if attempts < 1:
            raise ValueError("attempts must be at least 1")
        key = gemini_api_key(api_key)
        self.model = model or gemini_setting("GEMINI_VLM_MODEL", DEFAULT_VLM_MODEL)
        self.attempts = attempts
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
            f"菜品: {request.dish_name or '未指定'}\n"
            f"当前操作: {request.step_instruction or '未指定'}\n"
            f"静态完成条件: {request.completion_check}\n"
            f"相关物体: {', '.join(request.expected_objects) or '未指定'}\n"
            f"不可误判为完成的情况: "
            f"{'; '.join(request.failure_modes) or '没有额外说明'}"
        )
        if request.detection_context:
            prompt = f"{prompt}\n{request.detection_context}"
        response = None
        last_error: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                response = self._client.models.generate_content(
                    model=self.model,
                    contents=[
                        prompt,
                        types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                    ],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=_RESPONSE_SCHEMA,
                    ),
                )
                break
            except Exception as exc:  # noqa: BLE001 - SDK/network failures vary
                last_error = exc
                if attempt == self.attempts:
                    raise
                delay = 2 ** (attempt - 1)
                print(
                    f"Gemini VLM attempt {attempt}/{self.attempts} failed; "
                    f"retrying in {delay}s: {exc}"
                )
                time.sleep(delay)
        if response is None:
            assert last_error is not None
            raise last_error
        if not response.text:
            raise RuntimeError("Gemini VLM returned no structured text")
        return VLMObservation.model_validate_json(response.text)
