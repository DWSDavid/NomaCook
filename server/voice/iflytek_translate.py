"""iFLYTEK machine translation adapter for localizing spoken cues."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from email.utils import formatdate
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from server.iflytek_config import (
    IFlytekCredentials,
    iflytek_mt_credentials,
    normalize_language,
)


TRANSLATE_ENDPOINT = "https://itrans.xfyun.cn/v2/its"
TRANSLATE_HOST = "itrans.xfyun.cn"
TRANSLATE_PATH = "/v2/its"
MAX_BASE64_BYTES = 1_024


class IFlytekTranslationError(RuntimeError):
    pass


def translation_language_code(language: str) -> str:
    normalized = normalize_language(language)
    if normalized.lower() in {"zh-yue", "yue", "zh-hk"}:
        return "yue"
    if normalized.lower() == "zh" or normalized.lower().startswith("zh-"):
        return "cn"
    return normalized.split("-", 1)[0].lower()


def build_translation_body(
    credentials: IFlytekCredentials,
    text: str,
    *,
    source: str,
    target: str,
) -> bytes:
    cleaned = text.strip()
    if not cleaned:
        raise ValueError("translation text must not be empty")
    encoded = base64.b64encode(cleaned.encode("utf-8"))
    if len(encoded) > MAX_BASE64_BYTES:
        raise ValueError(
            f"translation input is {len(encoded)} base64 bytes; "
            f"iFLYTEK requires at most {MAX_BASE64_BYTES}"
        )
    payload = {
        "common": {"app_id": credentials.app_id},
        "business": {"from": source, "to": target},
        "data": {"text": encoded.decode("ascii")},
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def build_translation_headers(
    credentials: IFlytekCredentials,
    body: bytes,
    *,
    date: str | None = None,
) -> dict[str, str]:
    current_date = date or formatdate(usegmt=True)
    digest_value = "SHA-256=" + base64.b64encode(hashlib.sha256(body).digest()).decode(
        "ascii"
    )
    signature_origin = (
        f"host: {TRANSLATE_HOST}\n"
        f"date: {current_date}\n"
        f"POST {TRANSLATE_PATH} HTTP/1.1\n"
        f"digest: {digest_value}"
    )
    signature = base64.b64encode(
        hmac.new(
            credentials.api_secret.encode("utf-8"),
            signature_origin.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode("ascii")
    authorization = (
        f'api_key="{credentials.api_key}", algorithm="hmac-sha256", '
        f'headers="host date request-line digest", signature="{signature}"'
    )
    return {
        "Content-Type": "application/json",
        "Accept": "application/json,version=1.0",
        "Host": TRANSLATE_HOST,
        "Date": current_date,
        "Digest": digest_value,
        "Authorization": authorization,
    }


class IFlytekTranslator:
    def __init__(
        self,
        credentials: IFlytekCredentials | None = None,
        *,
        timeout: float = 10.0,
        opener: Any = urlopen,
    ) -> None:
        self.credentials = credentials or iflytek_mt_credentials()
        assert self.credentials is not None
        self.timeout = timeout
        self._opener = opener

    def translate(
        self,
        text: str,
        *,
        source: str = "cn",
        target: str,
    ) -> str:
        if source == target:
            return text
        body = build_translation_body(
            self.credentials, text, source=source, target=target
        )
        request = Request(
            TRANSLATE_ENDPOINT,
            data=body,
            headers=build_translation_headers(self.credentials, body),
            method="POST",
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                payload: dict[str, Any] = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError):
            raise IFlytekTranslationError(
                "讯飞翻译连接失败，请检查网络、系统时钟和 API 配置"
            ) from None
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise IFlytekTranslationError("讯飞翻译返回了无法解析的响应") from None
        try:
            code = int(payload.get("code", -1))
        except (TypeError, ValueError):
            raise IFlytekTranslationError("讯飞翻译返回了无效的状态码") from None
        if code != 0:
            message = str(payload.get("message") or "讯飞翻译请求失败")
            raise IFlytekTranslationError(f"{message} (code={code})")
        try:
            translated = payload["data"]["result"]["trans_result"]["dst"]
        except (KeyError, TypeError):
            raise IFlytekTranslationError("讯飞翻译响应缺少译文") from None
        result = str(translated).strip()
        if not result:
            raise IFlytekTranslationError("讯飞翻译返回了空译文")
        return result
