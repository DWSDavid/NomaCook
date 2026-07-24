"""Playback helpers for provider-neutral PCM streaming TTS."""

from __future__ import annotations

import asyncio

from server.voice.tts import SpeechRequest, StreamingTTSProvider


async def play_stream(
    provider: StreamingTTSProvider,
    request: SpeechRequest,
    *,
    output_device: int | str | None = None,
) -> int:
    import sounddevice as sd

    audio_format = provider.audio_format
    written = 0
    tail = b""
    frame_width = audio_format.channels * audio_format.sample_width
    try:
        sd.check_output_settings(
            device=output_device,
            channels=audio_format.channels,
            dtype="int16",
            samplerate=audio_format.sample_rate,
        )
    except sd.PortAudioError:
        raise RuntimeError(
            f"输出设备不接受 {audio_format.sample_rate} Hz PCM；"
            "请改用系统默认输出或支持该采样率的蓝牙设备"
        ) from None
    with sd.RawOutputStream(
        samplerate=audio_format.sample_rate,
        channels=audio_format.channels,
        dtype="int16",
        device=output_device,
    ) as stream:
        async for chunk in provider.stream(request):
            if not chunk:
                continue
            buffered = tail + chunk
            aligned_size = len(buffered) - (len(buffered) % frame_width)
            aligned, tail = buffered[:aligned_size], buffered[aligned_size:]
            if aligned:
                await asyncio.to_thread(stream.write, aligned)
                written += len(aligned)
    if tail:
        raise RuntimeError("TTS returned a truncated PCM frame")
    if written == 0:
        raise RuntimeError("TTS returned no audio")
    return written


def play_stream_sync(
    provider: StreamingTTSProvider,
    request: SpeechRequest,
    *,
    output_device: int | str | None = None,
) -> int:
    return asyncio.run(play_stream(provider, request, output_device=output_device))
