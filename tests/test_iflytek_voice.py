from __future__ import annotations

import asyncio
import base64
import json
import threading
import wave
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

import server.iflytek_config as iflytek_config
from perception.realtime_recognition import SpeechAnnouncer
from server.iflytek_config import IFlytekCredentials
from server.voice.iflytek_translate import (
    IFlytekTranslationError,
    IFlytekTranslator,
    build_translation_body,
    build_translation_headers,
    translation_language_code,
)
from server.voice.iflytek_tts import (
    IFlytekTTSError,
    IFlytekTTSProvider,
    build_auth_url,
    build_authorization,
    build_request_payload,
)
from server.voice.tts import AudioFormat, SpeechRequest, write_wav


CREDENTIALS = IFlytekCredentials(
    app_id="test_app_id",
    api_key="test_api_key",
    api_secret="test_api_secret",
)
DATE = "Thu, 01 Aug 2019 01:53:21 GMT"


def _request(text: str = "你好") -> SpeechRequest:
    return SpeechRequest(text=text, language="zh-CN", voice="x4_xiaoyan")


def test_chinese_voice_defaults_to_xiaojing(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(iflytek_config, "REPO_ENV_PATH", tmp_path / "missing.env")
    monkeypatch.delenv("IFLYTEK_TTS_VOICE_ZH_CN", raising=False)
    monkeypatch.delenv("IFLYTEK_TTS_VOICE", raising=False)

    assert iflytek_config.iflytek_tts_voice("zh-CN") == "aisjinger"


def test_tts_auth_matches_fixed_offline_vector():
    authorization = build_authorization(
        CREDENTIALS.api_key, CREDENTIALS.api_secret, DATE
    )
    assert authorization == (
        "YXBpX2tleT0idGVzdF9hcGlfa2V5IiwgYWxnb3JpdGhtPSJobWFjLXNoYTI1NiIs"
        "IGhlYWRlcnM9Imhvc3QgZGF0ZSByZXF1ZXN0LWxpbmUiLCBzaWduYXR1cmU9InVu"
        "eGNsQ3VkYlIwazNkZ2RRNkxWVWhpSlV3MlM4MXZHamV2WkJLY0VITGs9Ig=="
    )
    parsed = urlparse(build_auth_url(CREDENTIALS, date=DATE))
    assert parsed.hostname == "tts-api.xfyun.cn"
    query = parse_qs(parsed.query)
    assert query["host"] == ["tts-api.xfyun.cn"]
    assert query["date"] == [DATE]
    assert query["authorization"] == [authorization]


def test_tts_payload_is_utf8_base64_pcm16k_and_validates_limit():
    payload = build_request_payload(CREDENTIALS, _request("番茄炒蛋"))
    assert payload["common"] == {"app_id": "test_app_id"}
    assert payload["business"]["aue"] == "raw"
    assert payload["business"]["auf"] == "audio/L16;rate=16000"
    assert payload["business"]["tte"] == "UTF8"
    assert payload["business"]["speed"] == 50
    assert payload["business"]["volume"] == 50
    assert payload["business"]["pitch"] == 50
    assert payload["data"]["status"] == 2
    assert base64.b64decode(payload["data"]["text"]).decode() == "番茄炒蛋"

    tuned = build_request_payload(
        CREDENTIALS,
        SpeechRequest(
            text="番茄炒蛋",
            language="zh-CN",
            voice="x4_xiaoyan",
            speed=65,
            volume=45,
            pitch=42,
        ),
    )
    assert {
        key: tuned["business"][key] for key in ("speed", "volume", "pitch")
    } == {"speed": 65, "volume": 45, "pitch": 42}

    with pytest.raises(ValueError, match="fewer than 8000"):
        build_request_payload(CREDENTIALS, _request("a" * 8000))


class _FakeWebSocket:
    def __init__(self, messages: list[dict]):
        self.messages = [json.dumps(message) for message in messages]
        self.sent: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def send(self, payload: str):
        self.sent.append(payload)

    def __aiter__(self):
        self._iterator = iter(self.messages)
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration:
            raise StopAsyncIteration from None


def test_tts_stream_yields_audio_in_order_and_ignores_null_data():
    websocket = _FakeWebSocket(
        [
            {"code": 0, "message": "success", "sid": "sid-1", "data": None},
            {
                "code": 0,
                "message": "success",
                "data": {"audio": base64.b64encode(b"ab").decode(), "status": 1},
            },
            {
                "code": 0,
                "message": "success",
                "data": {"audio": base64.b64encode(b"cd").decode(), "status": 2},
            },
        ]
    )
    provider = IFlytekTTSProvider(
        CREDENTIALS, attempts=1, connector=lambda *args, **kwargs: websocket
    )

    async def collect() -> bytes:
        return b"".join([chunk async for chunk in provider.stream(_request())])

    assert asyncio.run(collect()) == b"abcd"
    assert json.loads(websocket.sent[0])["data"]["status"] == 2


def test_tts_business_error_is_safe_and_actionable():
    websocket = _FakeWebSocket(
        [{"code": 11200, "message": "no auth", "sid": "sid-2", "data": None}]
    )
    connection_attempts = 0

    def connector(*args, **kwargs):
        nonlocal connection_attempts
        connection_attempts += 1
        return websocket

    provider = IFlytekTTSProvider(CREDENTIALS, attempts=2, connector=connector)

    async def collect() -> None:
        _ = [chunk async for chunk in provider.stream(_request())]

    with pytest.raises(IFlytekTTSError, match="发音人未授权") as caught:
        asyncio.run(collect())
    assert "test_api_secret" not in str(caught.value)
    assert caught.value.code == 11200
    assert connection_attempts == 1


class _ChunkProvider:
    audio_format = AudioFormat()

    def __init__(self, chunks: list[bytes]):
        self.chunks = chunks

    async def stream(self, request: SpeechRequest):
        for chunk in self.chunks:
            yield chunk


def test_write_wav_reassembles_split_pcm_frames(tmp_path: Path):
    out = tmp_path / "voice.wav"
    written = asyncio.run(
        write_wav(_ChunkProvider([b"\x00", b"\x01\x02", b"\x03"]), _request(), out)
    )
    assert written == 4
    with wave.open(str(out), "rb") as stream:
        assert stream.getframerate() == 16_000
        assert stream.getnchannels() == 1
        assert stream.getsampwidth() == 2
        assert stream.readframes(2) == b"\x00\x01\x02\x03"


def test_write_wav_rejects_truncated_pcm(tmp_path: Path):
    with pytest.raises(RuntimeError, match="truncated PCM"):
        asyncio.run(
            write_wav(_ChunkProvider([b"\x00"]), _request(), tmp_path / "bad.wav")
        )
    assert not (tmp_path / "bad.wav").exists()


class _FakeHTTPResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def test_translation_signs_exact_body_and_returns_dst():
    body = build_translation_body(
        CREDENTIALS, "你好", source="cn", target="en"
    )
    headers = build_translation_headers(CREDENTIALS, body, date=DATE)
    assert headers["Digest"].startswith("SHA-256=")
    assert headers["Authorization"].startswith('api_key="test_api_key"')
    seen = {}

    def opener(request, timeout):
        seen["body"] = request.data
        seen["timeout"] = timeout
        return _FakeHTTPResponse(
            {
                "code": 0,
                "message": "success",
                "data": {
                    "result": {
                        "trans_result": {"src": "你好", "dst": "Hello"}
                    }
                },
            }
        )

    translator = IFlytekTranslator(CREDENTIALS, timeout=3.0, opener=opener)
    assert translator.translate("你好", source="cn", target="en") == "Hello"
    assert seen == {"body": body, "timeout": 3.0}
    assert translation_language_code("en-US") == "en"
    assert translation_language_code("zh-CN") == "cn"
    assert translation_language_code("zh-TW") == "cn"


@pytest.mark.parametrize("code", [None, "not-a-code"])
def test_translation_rejects_malformed_response_code(code):
    def opener(request, timeout):
        return _FakeHTTPResponse({"code": code})

    translator = IFlytekTranslator(CREDENTIALS, opener=opener)
    with pytest.raises(IFlytekTranslationError, match="无效的状态码"):
        translator.translate("你好", source="cn", target="en")


def test_speech_announcer_accepts_provider_callback():
    heard: list[str] = []
    done = threading.Event()

    def speaker(text: str) -> None:
        heard.append(text)
        done.set()

    announcer = SpeechAnnouncer(speaker=speaker)
    try:
        assert announcer.available and announcer.speak("鸡蛋已识别")
        assert done.wait(1.0)
        assert heard == ["鸡蛋已识别"]
    finally:
        announcer.close()


def test_cli_parsers_accept_iflytek_options():
    from harness.iflytek_tts_smoke import build_parser as smoke_parser
    from harness.live_recognition_demo import build_parser as live_parser
    from harness.run_pipeline import build_parser as pipeline_parser

    pipeline = pipeline_parser().parse_args(
        [
            "--source",
            "demo.mov",
            "--narrate",
            "iflytek",
            "--language",
            "en-US",
            "--iflytek-speed",
            "65",
            "--iflytek-volume",
            "45",
            "--iflytek-pitch",
            "42",
        ]
    )
    assert pipeline.narrate == "iflytek" and pipeline.language == "en-US"
    assert (
        pipeline.iflytek_speed,
        pipeline.iflytek_volume,
        pipeline.iflytek_pitch,
    ) == (65, 45, 42)
    live = live_parser().parse_args(
        [
            "--speech-backend",
            "iflytek",
            "--language",
            "ja-JP",
            "--iflytek-speed",
            "64",
        ]
    )
    assert live.speech_backend == "iflytek" and live.language == "ja-JP"
    assert (live.iflytek_speed, live.iflytek_volume, live.iflytek_pitch) == (
        64,
        50,
        50,
    )
    smoke = smoke_parser().parse_args([
        "--iflytek-speed",
        "63",
        "--iflytek-volume",
        "44",
        "--iflytek-pitch",
        "41",
    ])
    assert (smoke.iflytek_speed, smoke.iflytek_volume, smoke.iflytek_pitch) == (
        63,
        44,
        41,
    )


def test_cli_parsers_reject_out_of_range_iflytek_controls():
    from harness.iflytek_tts_smoke import build_parser as smoke_parser
    from harness.live_recognition_demo import build_parser as live_parser
    from harness.run_pipeline import build_parser as pipeline_parser

    cases = (
        (pipeline_parser(), ["--source", "demo.mov", "--iflytek-speed", "101"]),
        (live_parser(), ["--iflytek-volume", "-1"]),
        (smoke_parser(), ["--iflytek-pitch", "101"]),
    )
    for parser, argv in cases:
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


def test_pipeline_iflytek_preflight_fails_before_opening_video(
    tmp_path: Path, monkeypatch
):
    import server.iflytek_config as config
    from harness.run_pipeline import build_parser, run

    monkeypatch.setattr(config, "REPO_ENV_PATH", tmp_path / "missing.env")
    for name in ("IFLYTEK_APP_ID", "IFLYTEK_API_KEY", "IFLYTEK_API_SECRET"):
        monkeypatch.delenv(name, raising=False)
    args = build_parser().parse_args(
        ["--source", "does-not-exist.mov", "--narrate", "iflytek"]
    )
    with pytest.raises(SystemExit, match="iFLYTEK configuration error"):
        run(args)


def test_live_iflytek_preflight_fails_before_loading_detector(
    tmp_path: Path, monkeypatch
):
    import server.iflytek_config as config
    import harness.live_recognition_demo as live

    monkeypatch.setattr(config, "REPO_ENV_PATH", tmp_path / "missing.env")
    for name in ("IFLYTEK_APP_ID", "IFLYTEK_API_KEY", "IFLYTEK_API_SECRET"):
        monkeypatch.delenv(name, raising=False)

    class UnexpectedDetector:
        def __init__(self, *args, **kwargs):
            pytest.fail("detector loaded before iFLYTEK preflight")

    monkeypatch.setattr(live, "ObjectDetector", UnexpectedDetector)
    args = live.build_parser().parse_args(
        [
            "--source",
            "does-not-exist.mov",
            "--speech-backend",
            "iflytek",
            "--no-display",
        ]
    )
    with pytest.raises(RuntimeError, match="讯飞凭证未配置"):
        live.run(args)
