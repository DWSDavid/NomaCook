"""Shared iFLYTEK configuration with environment-first, local-.env fallback."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values


REPO_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
DEFAULT_TTS_VOICE_ZH_CN = "aisjinger"


@dataclass(frozen=True)
class IFlytekCredentials:
    app_id: str
    api_key: str
    api_secret: str


def iflytek_setting(name: str, default: str | None = None) -> str | None:
    """Read an iFLYTEK setting without requiring callers to preload ``.env``."""

    value = (os.environ.get(name) or "").strip()
    if value:
        return value
    if REPO_ENV_PATH.is_file():
        value = str(dotenv_values(REPO_ENV_PATH).get(name) or "").strip()
        if value:
            return value
    return default


def iflytek_credentials(*, required: bool = True) -> IFlytekCredentials | None:
    values = {
        "app_id": iflytek_setting("IFLYTEK_APP_ID"),
        "api_key": iflytek_setting("IFLYTEK_API_KEY"),
        "api_secret": iflytek_setting("IFLYTEK_API_SECRET"),
    }
    missing = [name for name, value in values.items() if not value]
    if missing and required:
        env_names = {
            "app_id": "IFLYTEK_APP_ID",
            "api_key": "IFLYTEK_API_KEY",
            "api_secret": "IFLYTEK_API_SECRET",
        }
        rendered = ", ".join(env_names[name] for name in missing)
        raise RuntimeError(
            f"讯飞凭证未配置：{rendered}。请写入仓库根目录的本地 .env；"
            "不要把密钥提交到 Git 或粘贴到聊天中。"
        )
    if missing:
        return None
    return IFlytekCredentials(
        app_id=str(values["app_id"]),
        api_key=str(values["api_key"]),
        api_secret=str(values["api_secret"]),
    )


def iflytek_mt_credentials(*, required: bool = True) -> IFlytekCredentials | None:
    """Resolve optional translation-specific keys, falling back to shared keys."""

    shared = iflytek_credentials(required=False)
    values = {
        "app_id": iflytek_setting(
            "IFLYTEK_MT_APP_ID", shared.app_id if shared else None
        ),
        "api_key": iflytek_setting(
            "IFLYTEK_MT_API_KEY", shared.api_key if shared else None
        ),
        "api_secret": iflytek_setting(
            "IFLYTEK_MT_API_SECRET", shared.api_secret if shared else None
        ),
    }
    missing = [name for name, value in values.items() if not value]
    if missing and required:
        raise RuntimeError(
            "讯飞翻译凭证未配置。可设置 IFLYTEK_MT_APP_ID / "
            "IFLYTEK_MT_API_KEY / IFLYTEK_MT_API_SECRET，或复用已开通翻译服务的"
            "通用讯飞凭证。"
        )
    if missing:
        return None
    return IFlytekCredentials(
        app_id=str(values["app_id"]),
        api_key=str(values["api_key"]),
        api_secret=str(values["api_secret"]),
    )


def iflytek_is_configured() -> bool:
    return iflytek_credentials(required=False) is not None


def normalize_language(language: str) -> str:
    """Normalize common CLI/API aliases to a compact BCP-47 form."""

    cleaned = language.strip().replace("_", "-")
    aliases = {
        "cn": "zh-CN",
        "zh": "zh-CN",
        "zh-cn": "zh-CN",
        "en": "en-US",
        "en-us": "en-US",
        "ja": "ja-JP",
        "ja-jp": "ja-JP",
        "ko": "ko-KR",
        "ko-kr": "ko-KR",
    }
    return aliases.get(cleaned.lower(), cleaned)


def _voice_env_name(language: str) -> str:
    suffix = re.sub(r"[^A-Za-z0-9]+", "_", normalize_language(language)).upper()
    return f"IFLYTEK_TTS_VOICE_{suffix}"


def iflytek_tts_voice(language: str, explicit: str | None = None) -> str:
    """Resolve a console-authorized voice for ``language``.

    iFLYTEK exposes language through the selected ``vcn`` voice rather than a
    separate request field. Non-Chinese languages therefore fail fast until a
    voice granted to the current app is configured explicitly.
    """

    if explicit and explicit.strip():
        return explicit.strip()
    normalized = normalize_language(language)
    configured = iflytek_setting(_voice_env_name(normalized))
    if configured:
        return configured
    configured = iflytek_setting("IFLYTEK_TTS_VOICE")
    if configured:
        return configured
    if normalized == "zh-CN":
        return DEFAULT_TTS_VOICE_ZH_CN
    raise RuntimeError(
        f"未配置 {normalized} 的讯飞发音人。请在控制台开通对应发音人，"
        f"并在 .env 设置 {_voice_env_name(normalized)}。"
    )
