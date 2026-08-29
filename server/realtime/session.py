"""Single-session lifecycle, media queues and Provider event adaptation."""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
import asyncio
import contextlib
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
        self._outbound: deque[tuple[str, object]] = deque()
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
        self._response_audio_announced = False
        self._closed_event_emitted = False
        self._pcm_buffer = bytearray()
        self._response_id: str | None = None
        self._pending_announce: dict[str, Any] | None = None
        self._announce_timer: asyncio.Task[None] | None = None

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
                self._remove_queued_audio()
                await self.provider.cancel_response()
                self._response_active = False
                self._audio_started = False
                self._pcm_buffer.clear()
                self._emit("response.interrupted", {})
            self._assistant_text_emitted = False
            self._emit("input.speech_started", {})
        elif event.type == "speech_stopped":
            self._emit("input.speech_stopped", {})
            self._emit("response.thinking", {})
        elif event.type == "assistant_text":
            text = event.text or ""
            if len(text) > 1000:
                self._emit_failure("MODEL_RESPONSE_INVALID", retryable=False, phase="provider_event")
                return
            if self._pending_announce is not None:
                if not self._response_matches(event.response_id):
                    self._emit_announce_failed("MODEL_RESPONSE_INVALID")
                    return
                self._pending_announce["transcript"] = text
            else:
                if self._assistant_text_emitted:
                    self._emit_failure("MODEL_RESPONSE_INVALID", retryable=False, phase="provider_event")
                    return
                self._assistant_text_emitted = True
                self._emit("response.assistant_text", {"text": text})
        elif event.type == "audio_started":
            if self._response_active:
                self._emit_failure("MODEL_RESPONSE_INVALID", retryable=False, phase="provider_event")
                return
            self._response_id = event.response_id or self._response_id
            self._response_active = True
        elif event.type == "audio_pcm":
            if event.pcm is None or self._paused:
                return
            if self._pending_announce is not None and not self._response_matches(event.response_id):
                self._emit_announce_failed("MODEL_RESPONSE_INVALID")
                return
            self._response_id = event.response_id or self._response_id
            self._response_active = True
            self._pcm_buffer.extend(event.pcm)
            frame_bytes = 480 * 2
            while len(self._pcm_buffer) >= frame_bytes:
                pcm_frame = bytes(self._pcm_buffer[:frame_bytes])
                del self._pcm_buffer[:frame_bytes]
                self._audio_started = True
                try:
                    opus = self.codec.encode_output_pcm(pcm_frame)
                except CodecError:
                    self._emit_failure("CODEC_UNAVAILABLE", retryable=False, phase="output_codec")
                    self._pcm_buffer.clear()
                    return
                if self._pending_announce is not None:
                    self._pending_announce["audio"].append(opus)
                    continue
                if self._audio_started and not self._response_audio_announced:
                    self._response_audio_announced = True
                    self._emit("response.audio_started", {})
                self._queue_audio(opus)
        elif event.type == "audio_done":
            await self._finish_response(event.response_id, status="completed")
        elif event.type == "response_done":
            await self._finish_response(
                event.response_id,
                status=event.status or "failed",
                error_code=event.error_code,
            )
        elif event.type == "error":
            if self._pending_announce is not None:
                self._emit_announce_failed(event.error_code or "ANNOUNCE_FAILED")
            else:
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
        if result:
            removed = set(result)
            self._outbound = deque(
                (kind, item)
                for kind, item in self._outbound
                if not (kind == "bytes" and item in removed)
            )
        if not self._events and not self._output_queue and not self._outbound:
            self._output_signal.clear()
        return result

    async def drain_outbound(self) -> list[tuple[str, object]]:
        result = list(self._outbound)
        self._outbound.clear()
        self._events.clear()
        self._output_queue.clear()
        self._output_signal.clear()
        return result

    async def wait_for_output(self) -> None:
        if self._outbound:
            return
        await self._output_signal.wait()

    async def close(self) -> None:
        if self._closed:
            return
        if self._announce_timer is not None:
            self._announce_timer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._announce_timer
            self._announce_timer = None
        await self.provider.stop()
        self._input_queue.clear()
        self._remove_queued_audio()
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
            response_id = await self.provider.announce(payload.model_dump(mode="json"))
        except Exception:
            self._emit("announce.failed", {"message_ref": payload.message_ref, "code": "ANNOUNCE_FAILED"})
            return
        self._pending_announce = {
            "utterance_id": payload.utterance_id,
            "message_ref": payload.message_ref,
            "text": payload.text,
            "deadline_at": payload.deadline_at,
            "response_id": response_id,
            "transcript": None,
            "audio": [],
        }
        if self._announce_timer is not None:
            self._announce_timer.cancel()
        self._announce_timer = asyncio.create_task(
            self._announce_timeout(response_id, payload.deadline_at),
            name="realtime-announce-timeout",
        )

    async def _handle_pause(self) -> None:
        self._require_started()
        self._paused = True
        self._input_queue.clear()
        self._remove_queued_audio()
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
        self._outbound.append(("text", envelope))
        self._output_signal.set()

    def _queue_audio(self, opus: bytes) -> None:
        audio = BinaryAudioFrame(
            "output_opus", self._next_output_sequence, self._next_output_timestamp, opus
        ).to_bytes()
        self._next_output_sequence = (self._next_output_sequence + 1) & 0xFFFFFFFF
        self._next_output_timestamp = (self._next_output_timestamp + 960) & 0xFFFFFFFF
        if len(self._output_queue) >= self._max_output_frames:
            self._output_queue.popleft()
            self._remove_one_audio_from_outbound()
            self._emit_failure("BACKPRESSURE", retryable=False, phase="output_queue")
        self._output_queue.append(audio)
        self._outbound.append(("bytes", audio))
        self._output_signal.set()

    def _remove_one_audio_from_outbound(self) -> None:
        for index, (kind, _) in enumerate(self._outbound):
            if kind == "bytes":
                del self._outbound[index]
                return

    def _remove_queued_audio(self) -> None:
        self._output_queue.clear()
        self._outbound = deque(
            (kind, item) for kind, item in self._outbound if kind != "bytes"
        )

    def _response_matches(self, response_id: str | None) -> bool:
        if self._pending_announce is None:
            return True
        expected = self._pending_announce["response_id"]
        return response_id is None or expected is None or response_id == expected

    async def _finish_response(
        self,
        response_id: str | None,
        *,
        status: str,
        error_code: str | None = None,
    ) -> None:
        if self._pending_announce is not None:
            if not self._response_matches(response_id):
                self._emit_announce_failed("MODEL_RESPONSE_INVALID")
                return
            pending = self._pending_announce
            if status != "completed":
                self._emit_announce_failed(error_code or "MODEL_RESPONSE_INVALID")
                return
            if (
                pending["transcript"] != pending["text"]
                or not pending["audio"]
                or self._pcm_buffer
            ):
                self._emit_announce_failed("ANNOUNCE_FAILED")
                return
            self._response_id = response_id or pending["response_id"]
            self._emit("response.audio_started", {})
            for opus in pending["audio"]:
                self._queue_audio(opus)
            self._emit("response.audio_done", {})
            self._emit(
                "announce.completed",
                {"utterance_id": pending["utterance_id"], "message_ref": pending["message_ref"]},
            )
            self._clear_pending_announce()
            self._reset_response()
            return
        if status != "completed" or not self._audio_started or self._pcm_buffer:
            self._remove_queued_audio()
            self._emit_failure(error_code or "MODEL_RESPONSE_INVALID", retryable=False, phase="provider_stream")
            self._reset_response()
            return
        self._emit("response.audio_done", {})
        self._reset_response()

    def _reset_response(self) -> None:
        self._response_active = False
        self._assistant_text_emitted = False
        self._audio_started = False
        self._response_id = None
        self._pcm_buffer.clear()
        self._response_audio_announced = False

    def _clear_pending_announce(self) -> None:
        if self._announce_timer is not None:
            self._announce_timer.cancel()
            self._announce_timer = None
        self._pending_announce = None

    def _emit_announce_failed(self, code: str) -> None:
        pending = self._pending_announce
        if pending is None:
            return
        self._remove_queued_audio()
        self._emit(
            "announce.failed",
            {"message_ref": pending["message_ref"], "code": code},
        )
        self._clear_pending_announce()
        self._reset_response()

    async def _announce_timeout(self, response_id: str, deadline: datetime) -> None:
        delay = max(0.0, (deadline - datetime.now(UTC)).total_seconds())
        await asyncio.sleep(delay)
        if self._pending_announce is not None and self._pending_announce["response_id"] == response_id:
            self._emit_announce_failed("MODEL_TIMEOUT")

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
