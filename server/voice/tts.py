"""Provider-neutral streaming text-to-speech contracts and WAV collection."""

from __future__ import annotations

import asyncio
import os
import tempfile
import wave
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class AudioFormat:
    sample_rate: int = 16_000
    channels: int = 1
    sample_width: int = 2
    encoding: str = "pcm_s16le"


@dataclass(frozen=True)
class SpeechRequest:
    text: str
    language: str
    voice: str
    speed: int = 50
    volume: int = 50
    pitch: int = 50


class StreamingTTSProvider(Protocol):
    audio_format: AudioFormat

    def stream(self, request: SpeechRequest) -> AsyncIterator[bytes]: ...


async def write_wav(
    provider: StreamingTTSProvider,
    request: SpeechRequest,
    out_path: Path,
) -> int:
    """Collect a PCM stream into an atomic, playable WAV file."""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(
        prefix=f".{out_path.name}.", suffix=".tmp", dir=out_path.parent
    )
    os.close(fd)
    tmp_path = Path(raw_tmp)
    written = 0
    tail = b""
    audio_format = provider.audio_format
    frame_width = audio_format.channels * audio_format.sample_width
    try:
        with wave.open(str(tmp_path), "wb") as stream:
            stream.setnchannels(audio_format.channels)
            stream.setsampwidth(audio_format.sample_width)
            stream.setframerate(audio_format.sample_rate)
            async for chunk in provider.stream(request):
                if not chunk:
                    continue
                buffered = tail + chunk
                aligned_size = len(buffered) - (len(buffered) % frame_width)
                aligned, tail = buffered[:aligned_size], buffered[aligned_size:]
                if aligned:
                    stream.writeframesraw(aligned)
                    written += len(aligned)
        if tail:
            raise RuntimeError("TTS returned a truncated PCM frame")
        if written == 0:
            raise RuntimeError("TTS returned no audio")
        os.replace(tmp_path, out_path)
        return written
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def write_wav_sync(
    provider: StreamingTTSProvider,
    request: SpeechRequest,
    out_path: Path,
) -> int:
    return asyncio.run(write_wav(provider, request, out_path))
