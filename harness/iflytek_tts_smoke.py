#!/usr/bin/env python3
"""One-sentence iFLYTEK translation + streaming TTS connectivity smoke."""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import wave
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from server.iflytek_config import (
    iflytek_credentials,
    iflytek_tts_voice,
    normalize_language,
)
from server.voice.iflytek_translate import (
    IFlytekTranslator,
    translation_language_code,
)
from server.voice.iflytek_tts import IFlytekTTSProvider
from server.voice.tts import SpeechRequest, StreamingTTSProvider, write_wav


DEFAULT_TEXT = "番茄炒蛋做好了。妈，我会做饭了。"


class _MeteredProvider:
    def __init__(self, provider: StreamingTTSProvider) -> None:
        self.provider = provider
        self.audio_format = provider.audio_format
        self.started_at = 0.0
        self.first_chunk_ms: float | None = None
        self.total_bytes = 0

    async def stream(self, request: SpeechRequest):
        self.started_at = time.perf_counter()
        async for chunk in self.provider.stream(request):
            if self.first_chunk_ms is None:
                self.first_chunk_ms = (time.perf_counter() - self.started_at) * 1000
            self.total_bytes += len(chunk)
            yield chunk


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument("--language", default="zh-CN")
    parser.add_argument("--voice", default=None, help="console-authorized vcn")
    parser.add_argument(
        "--iflytek-speed",
        type=int,
        choices=range(101),
        default=50,
        metavar="0-100",
        help="speaking speed (default: 50)",
    )
    parser.add_argument(
        "--iflytek-volume",
        type=int,
        choices=range(101),
        default=50,
        metavar="0-100",
        help="volume (default: 50)",
    )
    parser.add_argument(
        "--iflytek-pitch",
        type=int,
        choices=range(101),
        default=50,
        metavar="0-100",
        help="pitch (default: 50)",
    )
    parser.add_argument(
        "--out", default="/tmp/nomachef-iflytek-smoke.wav", help="output WAV"
    )
    parser.add_argument("--play", action="store_true", help="play the saved WAV")
    parser.add_argument("--output-device", default=None)
    return parser


def _audio_device(value: str | None) -> int | str | None:
    if value is None or not value.strip():
        return None
    return int(value) if value.isdigit() else value


def _play_wav(path: Path, output_device: int | str | None) -> None:
    import sounddevice as sd

    with wave.open(str(path), "rb") as audio:
        with sd.RawOutputStream(
            samplerate=audio.getframerate(),
            channels=audio.getnchannels(),
            dtype="int16",
            device=output_device,
        ) as stream:
            while chunk := audio.readframes(2048):
                stream.write(chunk)


async def run(args: argparse.Namespace) -> Path:
    language = normalize_language(args.language)
    voice = iflytek_tts_voice(language, args.voice)
    credentials = iflytek_credentials()
    assert credentials is not None
    target = translation_language_code(language)
    spoken_text = args.text
    translation_ms = 0.0
    if target != "cn":
        started = time.perf_counter()
        spoken_text = IFlytekTranslator().translate(
            args.text, source="cn", target=target
        )
        translation_ms = (time.perf_counter() - started) * 1000

    provider = _MeteredProvider(IFlytekTTSProvider(credentials))
    request = SpeechRequest(
        text=spoken_text,
        language=language,
        voice=voice,
        speed=args.iflytek_speed,
        volume=args.iflytek_volume,
        pitch=args.iflytek_pitch,
    )
    out_path = Path(args.out).expanduser().resolve()
    started = time.perf_counter()
    await write_wav(provider, request, out_path)
    total_ms = (time.perf_counter() - started) * 1000
    print(
        f"language={language} voice={voice} speed={request.speed} "
        f"volume={request.volume} pitch={request.pitch}"
    )
    print(f"spoken_text={spoken_text}")
    print(f"translation_ms={translation_ms:.1f}")
    print(f"first_audio_ms={provider.first_chunk_ms:.1f}")
    print(f"tts_total_ms={total_ms:.1f} audio_bytes={provider.total_bytes}")
    print(f"wav={out_path}")
    return out_path


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        out_path = asyncio.run(run(args))
        if args.play:
            _play_wav(out_path, _audio_device(args.output_device))
    except (RuntimeError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    main()
