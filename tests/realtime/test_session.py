from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
from typing import Any, AsyncIterator

import numpy as np
import pytest

from server.realtime.codec import OpusCodec
from server.realtime.contracts import BinaryAudioFrame
from server.realtime.provider import ProviderEvent
from server.realtime.session import RealtimeSession, SessionError


SESSION_ID = "11111111-1111-1111-1111-111111111111"


def _envelope(message_type: str, payload: dict[str, Any], *, seq: int, generation: int = 1) -> str:
    return json.dumps({
        "contract_version": "ai-realtime.contract.v1",
        "schema_version": "1.0",
        "session_id": SESSION_ID,
        "session_generation": generation,
        "producer": "backend",
        "stream_sequence": seq,
        "message_type": message_type,
        "occurred_at": "2026-08-29T10:00:00Z",
        "payload": payload,
    })


def _start(*, max_buffered_audio_ms: int = 1000, generation: int = 1, seq: int = 1) -> str:
    profile = {
        "encoding": "opus",
        "clock_rate_hz": 48000,
        "channels": 2,
        "frame_duration_ms": 20,
    }
    return _envelope(
        "session.start",
        {
            "started_at": "2026-08-29T10:00:00Z",
            "deadline_at": "2026-08-29T11:00:00Z",
            "input_audio": profile,
            "output_audio": profile,
            "context_revision": 1,
            "context": {},
            "limits": {"max_binary_frame_bytes": 65536, "max_buffered_audio_ms": max_buffered_audio_ms},
        },
        seq=seq,
        generation=generation,
    )


class FakeProvider:
    def __init__(self) -> None:
        self.started = False
        self.semantic_vad = False
        self.sent_pcm: list[bytes] = []
        self.announces: list[dict[str, Any]] = []
        self.contexts: list[tuple[int, dict[str, Any]]] = []
        self.cancel_count = 0
        self.pause_count = 0
        self.resume_count = 0
        self.stop_count = 0

    async def start(self, *, semantic_vad: bool, context: dict[str, Any]) -> None:
        self.started = True
        self.semantic_vad = semantic_vad

    async def send_audio(self, pcm16: bytes) -> None:
        self.sent_pcm.append(pcm16)

    async def update_context(self, revision: int, context: dict[str, Any]) -> None:
        self.contexts.append((revision, context))

    async def announce(self, payload: dict[str, Any]) -> None:
        self.announces.append(payload)

    async def cancel_response(self) -> None:
        self.cancel_count += 1

    async def pause(self) -> None:
        self.pause_count += 1

    async def resume(self) -> None:
        self.resume_count += 1

    async def stop(self) -> None:
        self.stop_count += 1

    async def events(self) -> AsyncIterator[ProviderEvent]:
        if False:
            yield ProviderEvent("noop")


def _run(coro):
    return asyncio.run(coro)


def test_session_start_vad_audio_events_and_codec_output() -> None:
    async def run() -> None:
        provider = FakeProvider()
        session = RealtimeSession(SESSION_ID, 1, provider=provider)
        await session.handle_text(_start())
        assert provider.started and provider.semantic_vad
        assert (await session.drain_events())[0].message_type == "session.ready"

        await session.handle_provider_event(ProviderEvent("speech_started"))
        await session.handle_provider_event(ProviderEvent("speech_stopped"))
        await session.handle_provider_event(ProviderEvent("assistant_text", text="继续。"))
        codec = OpusCodec()
        pcm24 = (np.zeros(480, dtype=np.int16) + 1000).tobytes()
        await session.handle_provider_event(ProviderEvent("audio_started"))
        await session.handle_provider_event(ProviderEvent("audio_pcm", pcm=pcm24))
        await session.handle_provider_event(ProviderEvent("audio_done"))
        events = await session.drain_events()
        assert [e.message_type for e in events] == [
            "input.speech_started",
            "input.speech_stopped",
            "response.thinking",
            "response.assistant_text",
            "response.audio_started",
            "response.audio_done",
        ]
        audio = await session.drain_audio()
        assert len(audio) == 1
        parsed = BinaryAudioFrame.from_bytes(audio[0])
        assert parsed.kind == "output_opus"
        assert parsed.packet_sequence == 1

    _run(run())


def test_input_audio_pause_stop_and_generation_boundaries() -> None:
    async def run() -> None:
        provider = FakeProvider()
        session = RealtimeSession(SESSION_ID, 1, provider=provider)
        await session.handle_text(_start())
        codec = OpusCodec()
        opus = codec.encode_opus(b"\x00" * (960 * 2 * 2), sample_rate=48000, channels=2)
        await session.handle_binary(BinaryAudioFrame("input_opus", 1, 0, opus).to_bytes())
        assert len(provider.sent_pcm) == 1
        await session.handle_text(_envelope("session.pause", {}, seq=2))
        await session.handle_binary(BinaryAudioFrame("input_opus", 2, 960, opus).to_bytes())
        assert len(provider.sent_pcm) == 1
        await session.handle_text(_envelope("session.resume", {}, seq=3))
        await session.handle_binary(BinaryAudioFrame("input_opus", 3, 1920, opus).to_bytes())
        assert len(provider.sent_pcm) == 2
        with pytest.raises(SessionError):
            await session.handle_text(_start(seq=4, generation=2))
        await session.handle_text(_envelope("session.stop", {"reason": "force_stop"}, seq=4))
        assert provider.stop_count == 1
        assert session.closed

    _run(run())


def test_interruption_announce_idempotency_and_backpressure() -> None:
    async def run() -> None:
        provider = FakeProvider()
        session = RealtimeSession(SESSION_ID, 1, provider=provider, max_output_frames=1)
        await session.handle_text(_start(max_buffered_audio_ms=20))
        announce = {"utterance_id": "u1", "message_ref": "m1", "text": "你好", "deadline_at": "2026-08-29T10:00:15Z"}
        await session.handle_text(_envelope("announce", announce, seq=2))
        await session.handle_text(_envelope("announce", announce, seq=3))
        assert len(provider.announces) == 1
        await session.handle_provider_event(ProviderEvent("speech_started"))
        await session.handle_provider_event(ProviderEvent("audio_started"))
        pcm = (np.zeros(480, dtype=np.int16) + 1000).tobytes()
        await session.handle_provider_event(ProviderEvent("audio_pcm", pcm=pcm))
        await session.handle_provider_event(ProviderEvent("audio_pcm", pcm=pcm))
        await session.handle_provider_event(ProviderEvent("speech_started"))
        events = await session.drain_events()
        assert "response.interrupted" in [e.message_type for e in events]
        assert provider.cancel_count == 1
        assert any(e.message_type == "session.failed" and e.payload.get("code") == "BACKPRESSURE" for e in events)

    _run(run())
