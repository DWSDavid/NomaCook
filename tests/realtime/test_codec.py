from __future__ import annotations

import asyncio

import numpy as np
import pytest

from server.realtime.codec import OpusCodec


def _sine(rate: int, samples: int, channels: int) -> bytes:
    t = np.arange(samples, dtype=np.float32) / rate
    mono = (np.sin(2 * np.pi * 440 * t) * 12000).astype(np.int16)
    if channels == 1:
        return mono.tobytes()
    return np.repeat(mono[:, None], channels, axis=1).reshape(-1).tobytes()


def test_real_opus_input_roundtrip_resamples_to_16khz_mono() -> None:
    codec = OpusCodec()
    encoded = codec.encode_opus(_sine(48000, 960, 2), sample_rate=48000, channels=2)
    pcm16 = codec.decode_input_opus(encoded)
    assert len(pcm16) == 320 * 2
    assert max(abs(int(v)) for v in np.frombuffer(pcm16, dtype=np.int16)) > 0


def test_real_opus_output_roundtrip_preserves_20ms_48khz_stereo_boundary() -> None:
    codec = OpusCodec()
    pcm24 = _sine(24000, 480, 1)
    encoded = codec.encode_output_pcm(pcm24)
    decoded = codec.decode_opus(encoded, sample_rate=48000, channels=2)
    assert len(decoded) == 960 * 2 * 2
    assert max(abs(int(v)) for v in np.frombuffer(decoded, dtype=np.int16)) > 0


def test_codec_rejects_wrong_pcm_frame_sizes() -> None:
    codec = OpusCodec()
    with pytest.raises(ValueError):
        codec.encode_output_pcm(b"\x00" * 10)
