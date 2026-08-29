"""Actual Opus and PCM16 codec bridge for the Realtime service."""

from __future__ import annotations

from typing import Iterable

import av
from av.audio.frame import AudioFrame
from av.audio.resampler import AudioResampler


INPUT_RATE = 16_000
OUTPUT_RATE = 24_000
RTP_RATE = 48_000
FRAME_DURATION_MS = 20
INPUT_SAMPLES = INPUT_RATE * FRAME_DURATION_MS // 1000
OUTPUT_SAMPLES = OUTPUT_RATE * FRAME_DURATION_MS // 1000
RTP_SAMPLES = RTP_RATE * FRAME_DURATION_MS // 1000
SAMPLE_BYTES = 2


class CodecError(ValueError):
    """An Opus/PCM frame cannot be decoded or encoded."""


class OpusCodec:
    """Stateful Opus codec with the contract's 20 ms audio profiles."""

    def __init__(self) -> None:
        try:
            self._input_decoder = self._new_codec("r", RTP_RATE, "stereo")
            self._output_encoder = self._new_codec("w", RTP_RATE, "stereo")
        except Exception as exc:  # pragma: no cover - depends on system codec install
            raise CodecError("Opus codec unavailable") from exc

    @staticmethod
    def _new_codec(mode: str, rate: int, layout: str) -> av.CodecContext:
        codec = av.CodecContext.create("opus", mode)
        codec.sample_rate = rate
        codec.layout = layout
        codec.format = "s16"
        codec.open()
        return codec

    def decode_input_opus(self, payload: bytes) -> bytes:
        if not payload:
            raise CodecError("Opus payload cannot be empty")
        try:
            frames = self._input_decoder.decode(av.Packet(payload))
            pcm = _resample_frames(frames, rate=INPUT_RATE, layout="mono")
        except Exception as exc:
            raise CodecError("invalid input Opus payload") from exc
        if len(pcm) != INPUT_SAMPLES * SAMPLE_BYTES:
            raise CodecError("input Opus frame is not 20 ms")
        return pcm

    def encode_output_pcm(self, pcm24k_mono: bytes) -> bytes:
        expected = OUTPUT_SAMPLES * SAMPLE_BYTES
        if len(pcm24k_mono) != expected:
            raise CodecError("output PCM frame must be 20 ms at 24 kHz mono")
        try:
            frame = _frame_from_pcm(
                pcm24k_mono,
                rate=OUTPUT_RATE,
                layout="mono",
                samples=OUTPUT_SAMPLES,
            )
            resampler = AudioResampler(format="s16", layout="stereo", rate=RTP_RATE)
            frames = _resample_with_flush(resampler, frame)
            if sum(item.samples for item in frames) != RTP_SAMPLES:
                raise CodecError("output resampler did not preserve 20 ms boundary")
            packets = []
            for item in frames:
                packets.extend(self._output_encoder.encode(item))
            encoded = b"".join(bytes(packet) for packet in packets)
        except CodecError:
            raise
        except Exception as exc:
            raise CodecError("cannot encode output Opus") from exc
        if not encoded:
            raise CodecError("Opus encoder produced an empty payload")
        return encoded

    def encode_opus(self, pcm: bytes, *, sample_rate: int, channels: int) -> bytes:
        """Encode one real 20 ms PCM frame for codec roundtrip tests."""

        samples = sample_rate * FRAME_DURATION_MS // 1000
        if sample_rate not in {OUTPUT_RATE, RTP_RATE} or channels not in {1, 2}:
            raise CodecError("unsupported PCM profile")
        if len(pcm) != samples * channels * SAMPLE_BYTES:
            raise CodecError("PCM frame is not 20 ms")
        codec = self._new_codec("w", sample_rate, "mono" if channels == 1 else "stereo")
        frame = _frame_from_pcm(
            pcm,
            rate=sample_rate,
            layout="mono" if channels == 1 else "stereo",
            samples=samples,
        )
        encoded = b"".join(bytes(packet) for packet in codec.encode(frame))
        if not encoded:
            raise CodecError("Opus encoder produced an empty payload")
        return encoded

    def decode_opus(self, payload: bytes, *, sample_rate: int, channels: int) -> bytes:
        if sample_rate != RTP_RATE or channels != 2 or not payload:
            raise CodecError("unsupported Opus decode profile")
        decoder = self._new_codec("r", sample_rate, "stereo")
        frames = decoder.decode(av.Packet(payload))
        pcm = _resample_frames(frames, rate=sample_rate, layout="stereo")
        if not pcm:
            raise CodecError("Opus decoder produced no PCM")
        return pcm


def _frame_from_pcm(pcm: bytes, *, rate: int, layout: str, samples: int) -> AudioFrame:
    frame = AudioFrame(format="s16", layout=layout, samples=samples)
    frame.sample_rate = rate
    frame.planes[0].update(pcm)
    return frame


def _resample_with_flush(resampler: AudioResampler, frame: AudioFrame) -> list[AudioFrame]:
    result = resampler.resample(frame)
    frames = result if isinstance(result, list) else [result]
    tail = resampler.resample(None)
    if tail:
        frames.extend(tail if isinstance(tail, list) else [tail])
    return frames


def _resample_frames(
    frames: Iterable[AudioFrame], *, rate: int, layout: str
) -> bytes:
    resampler = AudioResampler(format="s16", layout=layout, rate=rate)
    output: list[AudioFrame] = []
    for frame in frames:
        result = resampler.resample(frame)
        if result:
            output.extend(result if isinstance(result, list) else [result])
    tail = resampler.resample(None)
    if tail:
        output.extend(tail if isinstance(tail, list) else [tail])
    # ``AudioPlane`` may expose codec alignment padding through buffer_size;
    # ndarray uses the frame's logical sample count and avoids forwarding that
    # padding as PCM.
    return b"".join(frame.to_ndarray().tobytes() for frame in output)
