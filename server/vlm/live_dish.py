"""Conservative plated-dish recognition for the real-time demo."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Sequence

from google import genai
from google.genai import types

from server.gemini_config import gemini_api_key, gemini_setting


DEFAULT_MODEL = "gemini-3.6-flash"


@dataclass(frozen=True)
class LiveDishGuess:
    name: str
    confidence: float
    is_finished_dish: bool
    reason: str = ""


_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "is_finished_dish": types.Schema(type=types.Type.BOOLEAN),
        "dish_name": types.Schema(type=types.Type.STRING),
        "confidence": types.Schema(type=types.Type.NUMBER),
        "reason": types.Schema(type=types.Type.STRING),
    },
    required=["is_finished_dish", "dish_name", "confidence", "reason"],
)


def _prompt(candidates: Sequence[str]) -> str:
    candidate_text = "、".join(candidates)
    return (
        "你是 NomaCook 实时菜品识别器。判断画面里是否清晰出现了一道已经完成、"
        "可端上桌或已装盘的菜。原始食材、切配过程、空锅、刚下锅、单独炒蛋、"
        "半成品都必须判定 is_finished_dish=false；不要为了给答案而猜。"
        "只有菜品主体清晰、能可靠命名时才返回 true。"
        f"优先候选菜名：{candidate_text}。如果明显是其他菜，也可以给准确的中文菜名。"
        "dish_name 只写菜名；不是成品菜时写空字符串。confidence 范围 0 到 1，"
        "reason 用十个字以内说明画面依据。"
    )


def identify_live_dish(
    image_bytes: bytes,
    *,
    candidates: Sequence[str],
    api_key: str | None = None,
) -> LiveDishGuess:
    """Classify one JPEG frame; caller controls cadence and temporal gating."""

    if not image_bytes:
        raise ValueError("image_bytes cannot be empty")
    client = genai.Client(api_key=gemini_api_key(api_key))
    model = gemini_setting("GEMINI_VLM_MODEL", DEFAULT_MODEL)
    try:
        response = client.models.generate_content(
            model=model,
            contents=[
                _prompt(candidates),
                types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_SCHEMA,
            ),
        )
        data = json.loads(response.text or "{}")
        confidence = min(1.0, max(0.0, float(data.get("confidence") or 0.0)))
        is_finished = bool(data.get("is_finished_dish"))
        name = str(data.get("dish_name") or "").strip() if is_finished else ""
        return LiveDishGuess(
            name=name,
            confidence=confidence,
            is_finished_dish=is_finished and bool(name),
            reason=str(data.get("reason") or "").strip(),
        )
    finally:
        client.close()
