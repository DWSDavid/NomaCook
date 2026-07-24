"""Shared Gemini configuration with environment-first, local-.env fallback."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values


REPO_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def gemini_setting(name: str, default: str | None = None) -> str | None:
    """Read a Gemini setting without requiring callers to preload ``.env``."""

    value = (os.environ.get(name) or "").strip()
    if value:
        return value
    if REPO_ENV_PATH.is_file():
        value = str(dotenv_values(REPO_ENV_PATH).get(name) or "").strip()
        if value:
            return value
    return default


def gemini_api_key(api_key: str | None = None, *, required: bool = True) -> str | None:
    key = (api_key or gemini_setting("GEMINI_API_KEY") or "").strip()
    if not key and required:
        raise RuntimeError(
            "GEMINI_API_KEY 未配置。请把 key 写入仓库根目录的本地 .env；"
            "不要把 key 提交到 Git 或粘贴到聊天中。"
        )
    return key or None


def gemini_is_configured() -> bool:
    return gemini_api_key(required=False) is not None
