"""Grounded response plan: ensures Qwen cannot declare completion before StateEngine.

Pure functions — no side effects, no network, no state mutation.
"""

from __future__ import annotations

import re
from typing import Any, Literal

Intent = Literal["completion_query", "status_query", "next_step_query", "help", "general"]
ResponseStyle = Literal["concise", "step_prompt", "uncertain", "complete"]


COMPLETION_CLAIMS = re.compile(r"(已完成|已放入|放进去了|确认完成|确定完成|任务完成|进去了)")

COMPLETE_OVERRIDE_TEXT = (
    "还没有确认进去。我目前只确认到你已经到达冰箱前了。"
)


def classify_intent(user_transcript: str, snapshot: dict[str, Any] | None) -> Intent:
    if not user_transcript:
        return "general"
    t = user_transcript.strip().lower()
    if any(w in t for w in ("进去", "完成", "好了", "到了")):
        return "completion_query"
    if any(w in t for w in ("在哪", "哪一步", "做到哪", "什么步骤")):
        return "status_query"
    if any(w in t for w in ("然后", "下一步", "怎么做", "接下来")):
        return "next_step_query"
    if any(w in t for w in ("帮", "怎么", "为什么", "怎么办")):
        return "help"
    return "general"


def grounded_plan(
    *,
    snapshot: dict[str, Any] | None,
    user_transcript: str,
    recent_dialogue: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    intent = classify_intent(user_transcript, snapshot)
    status = snapshot.get("status", "ON_TRACK") if snapshot else "ON_TRACK"
    step_title = snapshot.get("step_title", "") if snapshot else ""
    step_instruction = snapshot.get("step_instruction", "") if snapshot else ""

    completion_allowed = status == "COMPLETE"

    plan: dict[str, Any] = {
        "intent": intent,
        "completion_allowed": completion_allowed,
        "required_fact": None,
        "optional_next_action": None,
        "response_style": "concise",
    }

    if intent == "completion_query":
        if completion_allowed:
            plan["required_fact"] = "StateEngine confirmed COMPLETE"
            plan["response_style"] = "complete"
        else:
            plan["required_fact"] = "尚未确认完成，当前只确认到: " + step_title
            plan["response_style"] = "uncertain"

    elif intent == "status_query":
        plan["required_fact"] = "current_step_title: " + step_title
        plan["response_style"] = "concise"

    elif intent == "next_step_query":
        plan["required_fact"] = "current_step_title: " + step_title
        plan["optional_next_action"] = step_instruction
        plan["response_style"] = "step_prompt"

    elif status == "COMPLETE":
        plan["required_fact"] = "StateEngine confirmed COMPLETE"
        plan["response_style"] = "complete"

    elif status == "UNCERTAIN":
        pq = snapshot.get("pending_question") if snapshot else None
        plan["required_fact"] = "UNCERTAIN: " + (pq or "waiting for evidence")
        plan["response_style"] = "uncertain"

    return plan


def check_fact_gate(
    *,
    assistant_transcript: str,
    snapshot: dict[str, Any] | None,
) -> tuple[bool, str | None]:
    """Returns (allowed_to_play, override_text_or_None)."""
    if snapshot is None:
        return True, None

    status = snapshot.get("status", "ON_TRACK")
    if status == "COMPLETE":
        return True, None

    if COMPLETION_CLAIMS.search(assistant_transcript):
        return False, COMPLETE_OVERRIDE_TEXT

    return True, None
