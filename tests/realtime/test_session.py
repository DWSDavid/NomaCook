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


def _envelope(
    message_type: str,
    payload: dict[str, Any],
    *,
    seq: int,
    generation: int = 1,
    occurred_at: str = "2026-08-29T10:00:00Z",
) -> str:
    return json.dumps({
        "contract_version": "ai-realtime.contract.v1",
        "schema_version": "1.1",
        "session_id": SESSION_ID,
        "session_generation": generation,
        "producer": "backend",
        "stream_sequence": seq,
        "message_type": message_type,
        "occurred_at": occurred_at,
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
        return f"response-{len(self.announces)}"

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


class FakeCodec:
    def __init__(self) -> None:
        self.frames: list[bytes] = []

    def encode_output_pcm(self, pcm: bytes) -> bytes:
        self.frames.append(pcm)
        return b"opus"

    def decode_input_opus(self, payload: bytes) -> bytes:
        return b"pcm"


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
        pcm24 = (np.zeros(960, dtype=np.int16) + 1000).tobytes()
        await session.handle_provider_event(ProviderEvent("audio_started"))
        await session.handle_provider_event(ProviderEvent("audio_pcm", pcm=pcm24))
        await session.handle_provider_event(ProviderEvent("response_done", status="completed"))
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
        assert len(audio) == 2
        parsed = [BinaryAudioFrame.from_bytes(frame) for frame in audio]
        assert [frame.kind for frame in parsed] == ["output_opus", "output_opus"]
        assert [frame.packet_sequence for frame in parsed] == [1, 2]

        outbound = await session.drain_outbound()
        assert [kind for kind, _ in outbound] == [
            "text", "text", "text", "text", "text", "bytes", "bytes", "text"
        ]


def test_session_ready_waits_for_provider_session_updated() -> None:
    async def run() -> None:
        provider = FakeProvider()
        gate = asyncio.Event()

        async def delayed_start(*, semantic_vad: bool, context: dict[str, Any]) -> None:
            provider.started = True
            provider.semantic_vad = semantic_vad
            await gate.wait()

        provider.start = delayed_start  # type: ignore[method-assign]
        session = RealtimeSession(SESSION_ID, 1, provider=provider)
        task = asyncio.create_task(session.handle_text(_start()))
        await asyncio.sleep(0)
        assert await session.drain_events() == []
        gate.set()
        await task
        events = await session.drain_events()
        assert [event.message_type for event in events] == ["session.ready"]

    _run(run())


def test_outbound_queue_keeps_audio_started_before_frames_and_done_after() -> None:
    async def run() -> None:
        provider = FakeProvider()
        session = RealtimeSession(SESSION_ID, 1, provider=provider)
        await session.handle_text(_start())
        await session.drain_outbound()
        await session.handle_provider_event(ProviderEvent("audio_started", response_id="r1"))
        await session.handle_provider_event(
            ProviderEvent("audio_pcm", pcm=(np.zeros(960, dtype=np.int16) + 1000).tobytes(), response_id="r1")
        )
        await session.handle_provider_event(ProviderEvent("response_done", status="completed", response_id="r1"))
        outbound = await session.drain_outbound()
        assert [kind for kind, _ in outbound] == ["text", "bytes", "bytes", "text"]
        assert [item.message_type for kind, item in outbound if kind == "text"] == [
            "response.audio_started",
            "response.audio_done",
        ]

    _run(run())


def test_pause_discards_unplayed_output_audio() -> None:
    async def run() -> None:
        provider = FakeProvider()
        session = RealtimeSession(SESSION_ID, 1, provider=provider)
        await session.handle_text(_start())
        await session.drain_outbound()
        await session.handle_provider_event(ProviderEvent("audio_started"))
        await session.handle_provider_event(
            ProviderEvent("audio_pcm", pcm=(np.zeros(480, dtype=np.int16) + 1000).tobytes())
        )
        await session.handle_text(_envelope("session.pause", {}, seq=2))
        assert all(kind != "bytes" for kind, _ in await session.drain_outbound())

    _run(run())

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
        pre_audio = await session.drain_events()
        assert all(event.message_type != "announce.completed" for event in pre_audio)
        provider = FakeProvider()
        session = RealtimeSession(SESSION_ID, 1, provider=provider, max_output_frames=1)
        await session.handle_text(_start(max_buffered_audio_ms=20))
        await session.drain_events()
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


def test_announce_releases_audio_only_after_exact_transcript_and_terminal() -> None:
    async def run() -> None:
        provider = FakeProvider()
        session = RealtimeSession(SESSION_ID, 1, provider=provider)
        await session.handle_text(_start())
        await session.drain_events()
        payload = {"utterance_id": "u1", "message_ref": "m1", "text": "继续翻炒", "deadline_at": "2026-08-29T10:00:15Z"}
        await session.handle_text(_envelope("announce", payload, seq=2))
        assert await session.drain_events() == []
        pcm = (np.zeros(480, dtype=np.int16) + 1000).tobytes()
        await session.handle_provider_event(ProviderEvent("assistant_text", text="继续翻炒", response_id="response-1"))
        await session.handle_provider_event(ProviderEvent("audio_started", response_id="response-1"))
        await session.handle_provider_event(ProviderEvent("audio_pcm", pcm=pcm, response_id="response-1"))
        assert await session.drain_audio() == []
        await session.handle_provider_event(ProviderEvent("response_done", status="completed", response_id="response-1"))
        events = await session.drain_events()
        assert events[-1].message_type == "announce.completed"
        audio = await session.drain_audio()
        assert len(audio) == 1

    _run(run())


def test_announce_mismatch_or_failed_response_discards_audio() -> None:
    async def run() -> None:
        for status, transcript in (("completed", "改写后的内容"), ("failed", "继续翻炒")):
            provider = FakeProvider()
            session = RealtimeSession(SESSION_ID, 1, provider=provider)
            await session.handle_text(_start())
            await session.drain_events()
            payload = {"utterance_id": "u1", "message_ref": "m1", "text": "继续翻炒", "deadline_at": "2026-08-29T10:00:15Z"}
            await session.handle_text(_envelope("announce", payload, seq=2))
            await session.handle_provider_event(ProviderEvent("assistant_text", text=transcript, response_id="response-1"))
            await session.handle_provider_event(ProviderEvent("audio_started", response_id="response-1"))
            await session.handle_provider_event(ProviderEvent("audio_pcm", pcm=(np.zeros(480, dtype=np.int16) + 1000).tobytes(), response_id="response-1"))
            await session.handle_provider_event(ProviderEvent("response_done", status=status, response_id="response-1"))
            events = await session.drain_events()
            assert events[-1].message_type == "announce.failed"
            assert await session.drain_audio() == []

    _run(run())


def test_failed_or_incomplete_response_done_never_emits_audio_done() -> None:
    async def run() -> None:
        for status, pcm in (("failed", b"\x00\x00" * 480), ("completed", b"\x00")):
            provider = FakeProvider()
            session = RealtimeSession(SESSION_ID, 1, provider=provider)
            await session.handle_text(_start())
            await session.drain_events()
            await session.handle_provider_event(ProviderEvent("audio_started", response_id="r1"))
            await session.handle_provider_event(ProviderEvent("audio_pcm", pcm=pcm, response_id="r1"))
            await session.handle_provider_event(ProviderEvent("response_done", status=status, response_id="r1"))
            events = await session.drain_events()
            assert events[-1].message_type == "session.failed"
            assert all(event.message_type != "response.audio_done" for event in events)
            assert await session.drain_audio() == []

    _run(run())


def test_v1_1_text_first_and_audio_first_share_utterance_id() -> None:
    async def run() -> None:
        pcm = b"\x01\x00" * 480
        for text_first in (True, False):
            provider = FakeProvider()
            codec = FakeCodec()
            session = RealtimeSession(SESSION_ID, 1, provider=provider, codec=codec)
            await session.handle_text(_start())
            await session.drain_events()
            if text_first:
                await session.handle_provider_event(
                    ProviderEvent("assistant_text", text="回答", response_id="r1")
                )
                await session.handle_provider_event(
                    ProviderEvent("audio_pcm", pcm=pcm, response_id="r1")
                )
            else:
                await session.handle_provider_event(
                    ProviderEvent("audio_pcm", pcm=pcm, response_id="r1")
                )
                await session.handle_provider_event(
                    ProviderEvent("assistant_text", text="回答", response_id="r1")
                )
            await session.handle_provider_event(
                ProviderEvent("response_done", status="completed", response_id="r1")
            )
            events = await session.drain_events()
            text_event = next(e for e in events if e.message_type == "response.assistant_text")
            started_event = next(e for e in events if e.message_type == "response.audio_started")
            done_event = next(e for e in events if e.message_type == "response.audio_done")
            assert text_event.payload["utterance_id"] == started_event.payload["utterance_id"]
            assert started_event.payload["utterance_id"] == done_event.payload["utterance_id"]
            assert done_event.payload["output_frame_count"] == 1
            assert done_event.payload.get("message_ref") is None

    _run(run())


def test_v1_1_partial_pcm_is_silence_padded_and_zero_audio_fails_closed() -> None:
    async def run() -> None:
        provider = FakeProvider()
        codec = FakeCodec()
        session = RealtimeSession(SESSION_ID, 1, provider=provider, codec=codec)
        await session.handle_text(_start())
        await session.drain_events()
        await session.handle_provider_event(
            ProviderEvent("audio_pcm", pcm=b"\x01\x00" * 100, response_id="r1")
        )
        await session.handle_provider_event(ProviderEvent("audio_done", response_id="r1"))
        await session.handle_provider_event(
            ProviderEvent("response_done", status="completed", response_id="r1")
        )
        events = await session.drain_events()
        assert events[-1].message_type == "response.audio_done"
        assert events[-1].payload["output_frame_count"] == 1
        assert len(codec.frames) == 1
        assert len(codec.frames[0]) == 960
        assert codec.frames[0][200:] == b"\x00" * 760

        empty_session = RealtimeSession(SESSION_ID, 1, provider=FakeProvider(), codec=FakeCodec())
        await empty_session.handle_text(_start())
        await empty_session.drain_events()
        await empty_session.handle_provider_event(
            ProviderEvent("response_done", status="completed", response_id="empty")
        )
        empty_events = await empty_session.drain_events()
        assert empty_events[-1].message_type == "session.failed"
        assert await empty_session.drain_audio() == []

    _run(run())


def test_v1_1_announce_releases_full_correlated_order_after_validation() -> None:
    async def run() -> None:
        provider = FakeProvider()
        codec = FakeCodec()
        session = RealtimeSession(SESSION_ID, 1, provider=provider, codec=codec)
        await session.handle_text(_start())
        await session.drain_outbound()
        payload = {
            "utterance_id": "u2",
            "message_ref": "m2",
            "text": "继续翻炒",
            "deadline_at": "2026-08-29T10:00:15Z",
        }
        await session.handle_text(_envelope("announce", payload, seq=2))
        await session.handle_provider_event(
            ProviderEvent("assistant_text", text="继续翻炒", response_id="response-1")
        )
        await session.handle_provider_event(
            ProviderEvent("audio_pcm", pcm=b"\x01\x00" * 100, response_id="response-1")
        )
        assert await session.drain_outbound() == []
        await session.handle_provider_event(
            ProviderEvent("response_done", status="completed", response_id="response-1")
        )
        outbound = await session.drain_outbound()
        assert [kind for kind, _ in outbound] == ["text", "text", "bytes", "text", "text"]
        text_event, started_event = outbound[0][1], outbound[1][1]
        done_event, completed_event = outbound[3][1], outbound[4][1]
        assert text_event.message_type == "response.assistant_text"
        assert text_event.payload == {
            "utterance_id": "u2",
            "message_ref": "m2",
            "text": "继续翻炒",
        }
        assert started_event.payload == {"utterance_id": "u2", "message_ref": "m2"}
        assert done_event.payload == {"utterance_id": "u2", "output_frame_count": 1}
        assert completed_event.payload == {"utterance_id": "u2", "message_ref": "m2"}

    _run(run())


def test_v1_1_output_packet_timeline_is_session_global() -> None:
    async def run() -> None:
        session = RealtimeSession(SESSION_ID, 1, provider=FakeProvider(), codec=FakeCodec())
        await session.handle_text(_start())
        await session.drain_events()
        for response_id in ("r1", "r2"):
            await session.handle_provider_event(
                ProviderEvent("audio_pcm", pcm=b"\x01\x00" * 480, response_id=response_id)
            )
            await session.handle_provider_event(
                ProviderEvent("response_done", status="completed", response_id=response_id)
            )
        outbound = await session.drain_outbound()
        frames = [BinaryAudioFrame.from_bytes(item) for kind, item in outbound if kind == "bytes"]
        assert [frame.packet_sequence for frame in frames] == [1, 2]
        assert [frame.rtp_timestamp for frame in frames] == [0, 960]
        envelopes = [item for kind, item in outbound if kind == "text"]
        assert [item.stream_sequence for item in envelopes] == list(
            range(1, len(envelopes) + 1)
        )
        assert all(item.schema_version == "1.1" for item in envelopes)
        assert all(
            earlier.occurred_at < later.occurred_at
            for earlier, later in zip(envelopes, envelopes[1:])
        )

    _run(run())


def test_v1_1_announce_without_audio_fails_closed() -> None:
    async def run() -> None:
        session = RealtimeSession(SESSION_ID, 1, provider=FakeProvider(), codec=FakeCodec())
        await session.handle_text(_start())
        await session.drain_events()
        payload = {
            "utterance_id": "u3",
            "message_ref": "m3",
            "text": "继续翻炒",
            "deadline_at": "2026-08-29T10:00:15Z",
        }
        await session.handle_text(_envelope("announce", payload, seq=2))
        await session.handle_provider_event(
            ProviderEvent("assistant_text", text="继续翻炒", response_id="response-1")
        )
        await session.handle_provider_event(
            ProviderEvent("audio_done", response_id="response-1")
        )
        await session.handle_provider_event(
            ProviderEvent("response_done", status="completed", response_id="response-1")
        )
        events = await session.drain_events()
        assert events[-1].message_type == "announce.failed"
        assert await session.drain_audio() == []

    _run(run())


def test_v1_1_backend_control_timestamp_cannot_move_backwards() -> None:
    async def run() -> None:
        session = RealtimeSession(SESSION_ID, 1, provider=FakeProvider(), codec=FakeCodec())
        await session.handle_text(_start())
        with pytest.raises(SessionError):
            await session.handle_text(
                _envelope(
                    "session.pause",
                    {},
                    seq=2,
                    occurred_at="2026-08-29T09:59:59Z",
                )
            )

    _run(run())


def test_interrupted_announce_quarantines_late_terminal_before_new_user_response() -> None:
    async def run() -> None:
        provider = FakeProvider()
        session = RealtimeSession(SESSION_ID, 1, provider=provider, codec=FakeCodec())
        await session.handle_text(_start())
        await session.drain_outbound()
        await session.handle_text(
            _envelope(
                "announce",
                {
                    "utterance_id": "u-announce",
                    "message_ref": "m-announce",
                    "text": "请继续",
                    "deadline_at": "2026-08-29T10:00:15Z",
                },
                seq=2,
            )
        )
        await session.handle_provider_event(
            ProviderEvent("audio_started", response_id="response-1")
        )
        await session.handle_provider_event(ProviderEvent("speech_started"))
        interrupted = await session.drain_events()
        assert [event.message_type for event in interrupted].count("announce.failed") == 1

        for event in (
            ProviderEvent("audio_pcm", pcm=b"\x01\x00" * 480, response_id="response-1"),
            ProviderEvent("audio_done", response_id="response-1"),
            ProviderEvent("response_done", status="cancelled", response_id="response-1"),
        ):
            await session.handle_provider_event(event)
        assert await session.drain_events() == []
        assert await session.drain_audio() == []

        await session.handle_provider_event(ProviderEvent("speech_stopped"))
        await session.handle_binary(BinaryAudioFrame("input_opus", 1, 0, b"opus").to_bytes())
        await session.handle_provider_event(
            ProviderEvent("assistant_text", text="用户回复", response_id="normal-r2")
        )
        await session.handle_provider_event(
            ProviderEvent("audio_pcm", pcm=b"\x01\x00" * 480, response_id="normal-r2")
        )
        await session.handle_provider_event(
            ProviderEvent("response_done", status="completed", response_id="normal-r2")
        )
        events = await session.drain_events()
        assert [event.message_type for event in events].count("response.thinking") == 1
        assert [event.message_type for event in events].count("response.assistant_text") == 1
        assert [event.message_type for event in events].count("response.audio_started") == 1
        assert [event.message_type for event in events].count("response.audio_done") == 1
        assert all(event.message_type != "session.failed" for event in events)
        text_event = next(
            event for event in events if event.message_type == "response.assistant_text"
        )
        assert text_event.payload["utterance_id"] != "u-announce"

        outbound = await session.drain_outbound()
        normal_items = [
            item
            for kind, item in outbound
            if kind == "bytes"
            or getattr(item, "message_type", None)
            in {"response.assistant_text", "response.audio_started", "response.audio_done"}
        ]
        assert [
            item.message_type if not isinstance(item, bytes) else "binary"
            for item in normal_items
        ] == ["response.assistant_text", "response.audio_started", "binary", "response.audio_done"]
        assert len([item for kind, item in outbound if kind == "bytes"]) == 1
        assert provider.sent_pcm == [b"pcm"]

    _run(run())
