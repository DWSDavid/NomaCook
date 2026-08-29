"""Single-session lifecycle, media queues and Provider event adaptation."""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
import asyncio
import json
from typing import Any
from uuid import UUID

from .codec import CodecError, OpusCodec
from .contracts import (
    AnnouncePayload,
    BinaryAudioFrame,
    ContextUpdatePayload,
    RealtimeEnvelope,
    SessionStartPayload,
    SessionStopPayload,
    parse_control_json,
)
from server.gateway.errors import ModelServiceError
from .provider import ProviderEvent, RealtimeProvider


class SessionError(RuntimeError):
    def __init__(self, code: str, message: str = "realtime session protocol error") -> None:
        super().__init__(message)
        self.code = code


class RealtimeSession:
    def __init__(
        self,
        session_id: str,
        generation: int,
        *,
        provider: RealtimeProvider,
        codec: OpusCodec | None = None,
        max_input_frames: int = 50,
        max_output_frames: int = 50,
    ) -> None:
        self.session_id = session_id
        self.generation = generation
        self.provider = provider
        self.codec = codec or OpusCodec()
        self._max_input_frames = max(1, max_input_frames)
        self._max_output_frames = max(1, max_output_frames)
        self._input_queue: deque[BinaryAudioFrame] = deque()
        self._output_queue: deque[bytes] = deque()
        self._events: deque[RealtimeEnvelope] = deque()
        self._output_signal = asyncio.Event()
        self._last_backend_sequence = 0
        self._backend_payloads: dict[int, str] = {}
        self._last_input_sequence: int | None = None
        self._last_input_timestamp: int | None = None
        self._next_output_sequence = 1
        self._next_output_timestamp = 0
        self._ai_sequence = 0
        self._started = False
        self._paused = False
        self._closed = False
        self._context_revision = 0
        self._context: dict[str, Any] = {}
        self._announces: dict[str, str] = {}
        self._response_active = False
        self._assistant_text_emitted = False
        self._audio_started = False
        self._closed_event_emitted = False

    @property
    def started(self) -> bool:
        return self._started

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def closed(self) -> bool:
        return self._closed

    async def handle_text(self, raw: str | bytes) -> None:
        envelope = parse_control_json(raw)
        if str(envelope.session_id) != self.session_id:
            raise SessionError("SESSION_NOT_ACTIVE")
        if envelope.session_generation != self.generation:
            raise SessionError("GENERATION_STALE")
        if envelope.producer != "backend":
            raise SessionError("INVALID_REQUEST")
        payload_fingerprint = _fingerprint(envelope.payload)
        if envelope.stream_sequence <= self._last_backend_sequence:
            previous = self._backend_payloads.get(envelope.stream_sequence)
            if previous == payload_fingerprint:
                return
            raise SessionError("INVALID_REQUEST", "conflicting control sequence")
        if envelope.stream_sequence != self._last_backend_sequence + 1:
            raise SessionError("INVALID_REQUEST", "control sequence is not contiguous")
        self._last_backend_sequence = envelope.stream_sequence
        self._backend_payloads[envelope.stream_sequence] = payload_fingerprint

        if envelope.message_type == "session.start":
            await self._handle_start(envelope)
        elif envelope.message_type == "context.update":
            await self._handle_context_update(envelope)
        elif envelope.message_type == "announce":
            await self._handle_announce(envelope)
        elif envelope.message_type == "session.pause":
            await self._handle_pause()
        elif envelope.message_type == "session.resume":
            await self._handle_resume()
        elif envelope.message_type == "session.stop":
            await self._handle_stop(envelope)
        else:
            raise SessionError("INVALID_REQUEST", "message is not a Backend control message")

    async def handle_binary(self, raw: bytes) -> None:
        if not self._started or self._closed:
            raise SessionError("SESSION_NOT_ACTIVE")
        frame = BinaryAudioFrame.from_bytes(raw)
        if frame.kind != "input_opus":
            raise SessionError("INVALID_REQUEST", "Backend may only send input Opus")
        if self._last_input_sequence is not None:
            if frame.packet_sequence != (self._last_input_sequence + 1) & 0xFFFFFFFF:
                raise SessionError("INVALID_REQUEST", "input packet sequence is not contiguous")
            if (
                self._last_input_timestamp is None
                or (frame.rtp_timestamp - self._last_input_timestamp) & 0xFFFFFFFF != 960
            ):
                raise SessionError("INVALID_REQUEST", "input RTP timestamp is not 20 ms apart")
        self._last_input_sequence = frame.packet_sequence
        self._last_input_timestamp = frame.rtp_timestamp
        if self._paused:
            return
        if len(self._input_queue) >= self._max_input_frames:
            self._input_queue.popleft()
            self._emit_failure("BACKPRESSURE", retryable=False, phase="input_queue")
        self._input_queue.append(frame)
        await self._drain_input()

    async def handle_provider_event(self, event: ProviderEvent) -> None:
        if self._closed:
            return
        if event.type == "ready":
            return
        if event.type == "speech_started":
            if self._response_active:
                self._output_queue.clear()
                await self.provider.cancel_response()
                self._response_active = False
                self._audio_started = False
                self._emit("response.interrupted", {})
            self._assistant_text_emitted = False
            self._emit("input.speech_started", {})
        elif event.type == "speech_stopped":
            self._emit("input.speech_stopped", {})
            self._emit("response.thinking", {})
        elif event.type == "assistant_text":
            text = event.text or ""
            if self._assistant_text_emitted or len(text) > 1000:
                self._emit_failure("MODEL_RESPONSE_INVALID", retryable=False, phase="provider_event")
                return
            self._assistant_text_emitted = True
            self._emit("response.assistant_text", {"text": text})
        elif event.type == "audio_started":
            if self._audio_started:
                self._emit_failure("MODEL_RESPONSE_INVALID", retryable=False, phase="provider_event")
                return
            self._audio_started = True
            self._response_active = True
            self._emit("response.audio_started", {})
        elif event.type == "audio_pcm":
            if not self._audio_started or event.pcm is None or self._paused:
                return
            try:
                opus = self.codec.encode_output_pcm(event.pcm)
            except CodecError:
                self._emit_failure("CODEC_UNAVAILABLE", retryable=False, phase="output_codec")
                return
            audio = BinaryAudioFrame(
                "output_opus", self._next_output_sequence, self._next_output_timestamp, opus
            ).to_bytes()
            self._next_output_sequence = (self._next_output_sequence + 1) & 0xFFFFFFFF
            self._next_output_timestamp = (self._next_output_timestamp + 960) & 0xFFFFFFFF
            if len(self._output_queue) >= self._max_output_frames:
                self._output_queue.popleft()
                self._emit_failure("BACKPRESSURE", retryable=False, phase="output_queue")
            self._output_queue.append(audio)
            self._output_signal.set()
        elif event.type == "audio_done":
            if self._audio_started:
                self._emit("response.audio_done", {})
            self._response_active = False
            self._audio_started = False
        elif event.type == "error":
            self._emit_failure(
                event.error_code or "AI_REALTIME_ERROR",
                retryable=False,
                phase="provider_stream",
            )

    async def run_provider_events(self) -> None:
        async for event in self.provider.events():
            await self.handle_provider_event(event)
            if self._closed:
                return

    async def drain_events(self) -> list[RealtimeEnvelope]:
        result = list(self._events)
        self._events.clear()
        if not self._output_queue:
            self._output_signal.clear()
        return result

    async def drain_audio(self) -> list[bytes]:
        result = list(self._output_queue)
        self._output_queue.clear()
        if not self._output_queue:
            self._output_signal.clear()
        return result

    async def wait_for_output(self) -> None:
        if self._events or self._output_queue:
            return
        await self._output_signal.wait()

    async def close(self) -> None:
        if self._closed:
            return
        await self.provider.stop()
        self._input_queue.clear()
        self._output_queue.clear()
        if not self._closed_event_emitted:
            self._emit("session.closed", {})
            self._closed_event_emitted = True
        self._closed = True

    async def _handle_start(self, envelope: RealtimeEnvelope) -> None:
        if self._started or envelope.stream_sequence != 1:
            raise SessionError("INVALID_REQUEST")
        payload = SessionStartPayload.model_validate(envelope.payload, strict=False)
        self._context_revision = payload.context_revision
        self._context = dict(payload.context)
        self._max_input_frames = max(1, payload.limits.max_buffered_audio_ms // 20)
        self._max_output_frames = max(1, payload.limits.max_buffered_audio_ms // 20)
        await self.provider.start(semantic_vad=True, context=self._context)
        self._started = True
        self._emit("session.ready", {"context_revision": self._context_revision})

    async def _handle_context_update(self, envelope: RealtimeEnvelope) -> None:
        self._require_started()
        payload = ContextUpdatePayload.model_validate(envelope.payload, strict=False)
        fingerprint = _fingerprint(payload.context)
        if payload.context_revision < self._context_revision:
            return
        if payload.context_revision == self._context_revision:
            if fingerprint != _fingerprint(self._context):
                raise SessionError("INVALID_REQUEST", "conflicting context revision")
            return
        self._context_revision = payload.context_revision
        self._context = dict(payload.context)
        await self.provider.update_context(self._context_revision, self._context)

    async def _handle_announce(self, envelope: RealtimeEnvelope) -> None:
        self._require_started()
        payload = AnnouncePayload.model_validate(envelope.payload, strict=False)
        fingerprint = _fingerprint(payload.model_dump(mode="json"))
        previous = self._announces.get(payload.message_ref)
        if previous is not None:
            if previous == fingerprint:
                return
            self._emit_failure("IDEMPOTENCY_CONFLICT", retryable=False, phase="announce")
            return
        self._announces[payload.message_ref] = fingerprint
        try:
            await self.provider.announce(payload.model_dump(mode="json"))
        except Exception:
            self._emit("announce.failed", {"message_ref": payload.message_ref, "code": "ANNOUNCE_FAILED"})
            return
        self._emit(
            "announce.completed",
            {"utterance_id": payload.utterance_id, "message_ref": payload.message_ref},
        )

    async def _handle_pause(self) -> None:
        self._require_started()
        self._paused = True
        self._input_queue.clear()
        self._output_queue.clear()
        self._output_signal.clear()
        await self.provider.pause()

    async def _handle_resume(self) -> None:
        self._require_started()
        if self._closed:
            raise SessionError("SESSION_NOT_ACTIVE")
        self._paused = False
        await self.provider.resume()

    async def _handle_stop(self, envelope: RealtimeEnvelope) -> None:
        self._require_started()
        SessionStopPayload.model_validate(envelope.payload, strict=False)
        await self.close()

    async def _drain_input(self) -> None:
        while self._input_queue and not self._paused and not self._closed:
            frame = self._input_queue.popleft()
            try:
                pcm = self.codec.decode_input_opus(frame.payload)
            except CodecError:
                self._emit_failure("CODEC_UNAVAILABLE", retryable=False, phase="input_codec")
                continue
            await self.provider.send_audio(pcm)

    def _require_started(self) -> None:
        if not self._started or self._closed:
            raise SessionError("SESSION_NOT_ACTIVE")

    def _emit(self, message_type: str, payload: dict[str, Any]) -> None:
        self._ai_sequence += 1
        envelope = RealtimeEnvelope(
            contract_version="ai-realtime.contract.v1",
            schema_version="1.0",
            session_id=UUID(self.session_id),
            session_generation=self.generation,
            producer="ai_service",
            stream_sequence=self._ai_sequence,
            message_type=message_type,
            occurred_at=datetime.now(UTC),
            payload=payload,
        )
        self._events.append(envelope)
        self._output_signal.set()

    def _emit_failure(self, code: str, *, retryable: bool, phase: str) -> None:
        self._emit(
            "session.failed",
            {
                "code": code,
                "retryable": retryable,
                "phase": phase,
                "message": "realtime operation failed",
            },
        )


def _fingerprint(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
