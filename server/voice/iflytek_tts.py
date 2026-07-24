"""iFLYTEK online streaming TTS WebSocket adapter."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from collections.abc import AsyncIterator, Callable
from email.utils import formatdate
from typing import Any
from urllib.parse import urlencode

from websockets.asyncio.client import connect

from server.iflytek_config import IFlytekCredentials, iflytek_credentials
from server.voice.tts import AudioFormat, SpeechRequest


TTS_ENDPOINT = "wss://tts-api.xfyun.cn/v2/tts"
TTS_HOST = "tts-api.xfyun.cn"
TTS_PATH = "/v2/tts"
MAX_TEXT_BYTES = 7_999


class IFlytekTTSError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        sid: str | None = None,
    ) -> None:
        details = message
        if code is not None:
            details = f"{details} (code={code})"
        if sid:
            details = f"{details} (sid={sid})"
        super().__init__(details)
        self.code = code
        self.sid = sid


def _signature(api_secret: str, date: str) -> str:
    origin = f"host: {TTS_HOST}\ndate: {date}\nGET {TTS_PATH} HTTP/1.1"
    digest = hmac.new(
        api_secret.encode("utf-8"), origin.encode("utf-8"), hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def build_authorization(api_key: str, api_secret: str, date: str) -> str:
    signature = _signature(api_secret, date)
    origin = (
        f'api_key="{api_key}", algorithm="hmac-sha256", '
        f'headers="host date request-line", signature="{signature}"'
    )
    return base64.b64encode(origin.encode("utf-8")).decode("ascii")


def build_auth_url(credentials: IFlytekCredentials, *, date: str | None = None) -> str:
    current_date = date or formatdate(usegmt=True)
    query = urlencode(
        {
            "authorization": build_authorization(
                credentials.api_key, credentials.api_secret, current_date
            ),
            "date": current_date,
            "host": TTS_HOST,
        }
    )
    return f"{TTS_ENDPOINT}?{query}"


def build_request_payload(
    credentials: IFlytekCredentials, request: SpeechRequest
) -> dict[str, Any]:
    text = request.text.strip()
    if not text:
        raise ValueError("TTS text must not be empty")
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_TEXT_BYTES:
        raise ValueError(
            f"TTS text is {len(encoded)} UTF-8 bytes; iFLYTEK requires fewer than 8000"
        )
    for name, value in (
        ("speed", request.speed),
        ("volume", request.volume),
        ("pitch", request.pitch),
    ):
        if not 0 <= value <= 100:
            raise ValueError(f"{name} must be between 0 and 100")
    return {
        "common": {"app_id": credentials.app_id},
        "business": {
            "aue": "raw",
            "auf": "audio/L16;rate=16000",
            "vcn": request.voice,
            "tte": "UTF8",
            "speed": request.speed,
            "volume": request.volume,
            "pitch": request.pitch,
        },
        "data": {
            "status": 2,
            "text": base64.b64encode(encoded).decode("ascii"),
        },
    }


class IFlytekTTSProvider:
    audio_format = AudioFormat()

    def __init__(
        self,
        credentials: IFlytekCredentials | None = None,
        *,
        attempts: int = 2,
        open_timeout: float = 8.0,
        total_timeout: float = 30.0,
        connector: Callable[..., Any] = connect,
    ) -> None:
        if attempts < 1:
            raise ValueError("attempts must be at least 1")
        self.credentials = credentials or iflytek_credentials()
        assert self.credentials is not None
        self.attempts = attempts
        self.open_timeout = open_timeout
        self.total_timeout = total_timeout
        self._connector = connector

    async def _stream_once(self, request: SpeechRequest) -> AsyncIterator[bytes]:
        payload = build_request_payload(self.credentials, request)
        url = build_auth_url(self.credentials)
        completed = False
        try:
            async with asyncio.timeout(self.total_timeout):
                async with self._connector(
                    url,
                    open_timeout=self.open_timeout,
                    close_timeout=3.0,
                    max_size=2**20,
                ) as websocket:
                    await websocket.send(json.dumps(payload, ensure_ascii=False))
                    async for raw_message in websocket:
                        if not isinstance(raw_message, str):
                            raise IFlytekTTSError(
                                "讯飞 TTS 返回了未启用的二进制协议"
                            )
                        response = json.loads(raw_message)
                        code = int(response.get("code", -1))
                        sid = response.get("sid")
                        if code != 0:
                            message = str(response.get("message") or "讯飞 TTS 请求失败")
                            if code == 11200:
                                message = "讯飞发音人未授权，请在控制台开通当前 vcn"
                            raise IFlytekTTSError(message, code=code, sid=sid)
                        data = response.get("data")
                        if not data:
                            continue
                        audio = data.get("audio")
                        if audio:
                            try:
                                yield base64.b64decode(audio, validate=True)
                            except (ValueError, TypeError) as exc:
                                raise IFlytekTTSError(
                                    "讯飞 TTS 返回了无效的音频编码", sid=sid
                                ) from exc
                        if int(data.get("status", 0)) == 2:
                            completed = True
                            break
        except IFlytekTTSError:
            raise
        except TimeoutError:
            raise IFlytekTTSError("讯飞 TTS 请求超时") from None
        except Exception:
            # Do not surface the signed WebSocket URL or credential-bearing query.
            raise IFlytekTTSError(
                "讯飞 TTS 连接失败，请检查网络、系统时钟和 API 配置"
            ) from None
        if not completed:
            raise IFlytekTTSError("讯飞 TTS 连接提前结束，未收到完成帧")

    async def stream(self, request: SpeechRequest) -> AsyncIterator[bytes]:
        for attempt in range(1, self.attempts + 1):
            yielded = False
            try:
                async for chunk in self._stream_once(request):
                    yielded = True
                    yield chunk
                return
            except IFlytekTTSError as exc:
                # Business errors are deterministic; retry only connection-level
                # failures that happened before any audio reached the caller.
                if yielded or exc.code is not None or attempt == self.attempts:
                    raise
                await asyncio.sleep(0.25 * attempt)
