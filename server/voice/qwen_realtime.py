"""Qwen3 Omni Realtime adapter via native WebSocket.

Half-duplex: mic muted + queue cleared during assistant playback.
Fact gate: transcript validated against StateEngine before audio plays.
Dialogue memory: HotMemory holds 6 recent turns for grounded responses.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sounddevice as sd
import websockets
from websockets.exceptions import ConnectionClosed

from .grounded import check_fact_gate, grounded_plan

INPUT_SAMPLE_RATE = 16000
OUTPUT_SAMPLE_RATE = 24000
CHUNK_MS = 100
INPUT_CHUNK = int(INPUT_SAMPLE_RATE * CHUNK_MS / 1000)
MAX_CONNECT_ATTEMPTS = 2
RECONNECT_BACKOFF = 2.0
MIC_RESUME_COOLDOWN_S = 0.5

QWEN_SYSTEM_INSTRUCTION = """你是 NomaCook 的任务辅助者。
你无法看到原始画面，只能依据 [TASK_STATE] 中的信息回答。
不要声称你能看到、检查或做了任何视觉或物理动作。
StateEngine 决定是否完成任务，你不能自行宣布。

回答规则：
- 用户问完成任务相关的问题时，只根据 [TASK_STATE] 的 status 和 current_step_title 回答
- 用户问怎么继续时，才提供 instruction
- ON_TRACK 且用户没问时不主动讲话
- UNCERTAIN 时只问 pending_question
- COMPLETE 时简短宣布
- 直接回答，不反问，不复述用户整句话
- 每次最多两句话"""


@dataclass
class RealtimeEventLog:
    events: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def append(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self.events.append(entry)

    def flush_to(self, path: Path) -> None:
        with self._lock:
            if not self.events:
                return
            with path.open("a", encoding="utf-8") as f:
                for e in self.events:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
            self.events.clear()


def _check_env() -> None:
    missing = []
    for var in ("DASHSCOPE_API_KEY", "BAILIAN_WORKSPACE_ID"):
        if not os.getenv(var):
            missing.append(var)
    if missing:
        raise RuntimeError(
            f"Missing environment variables: {', '.join(missing)}."
        )


def _build_url(model: str, url_override: str | None = None) -> str:
    ws_id = os.environ["BAILIAN_WORKSPACE_ID"]
    if url_override:
        return url_override
    return f"wss://{ws_id}.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime?model={model}"


def _clear_q(q: asyncio.Queue) -> None:
    with contextlib.suppress(asyncio.QueueEmpty):
        while True:
            q.get_nowait()


def _resolve_model_defaults(model: str, voice_override: str | None = None) -> tuple[str, str]:
    if model.startswith("qwen3.5"):
        vad_type = "semantic_vad"
        voice = "Tina"
    else:
        vad_type = "server_vad"
        voice = "Cherry"
    if voice_override is not None:
        voice = voice_override
    return voice, vad_type


def _snapshot_dict(hot_memory) -> dict[str, Any] | None:
    if hot_memory is None:
        return None
    mem = hot_memory.read()
    return mem.get("snapshot")


class QwenRealtimeAdapter:

    def __init__(
        self,
        *,
        model: str = "qwen3-omni-flash-realtime",
        voice: str | None = None,
        url_override: str | None = None,
        hot_memory=None,
        session_dir: Path | None = None,
    ):
        _check_env()
        self._model = model
        res_v, res_vad = _resolve_model_defaults(model, voice)
        self._voice = res_v
        self._vad_type = res_vad
        self._ws_url = _build_url(model, url_override)
        self._api_key = os.environ["DASHSCOPE_API_KEY"]
        self._hot = hot_memory
        self._session_dir = session_dir

        self._stop = asyncio.Event()
        self._connected = False
        self._error_count = 0
        self._user_turns = 0
        self._assistant_turns = 0
        self._first_audio_lats: list[float] = []
        self._last_sig: tuple | None = None

        self._event_log = RealtimeEventLog()
        if session_dir:
            (session_dir / "realtime_events.jsonl").touch()

        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def error_count(self) -> int:
        return self._error_count

    def stats(self) -> dict[str, Any]:
        lats = list(self._first_audio_lats)
        return {
            "user_turn_count": self._user_turns,
            "assistant_turn_count": self._assistant_turns,
            "qwen_first_audio_mean_ms": round(sum(lats) / len(lats), 2) if lats else None,
            "qwen_first_audio_p95_ms": round(self._p95(lats), 2) if lats else None,
            "qwen_error_count": self._error_count,
        }

    @staticmethod
    def _p95(vals):
        if not vals:
            return 0
        s = sorted(vals)
        return s[int((len(s) - 1) * 0.95)]

    def request_stop(self) -> None:
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._stop.set)
        else:
            self._stop.set()

    def _log(self, entry: dict[str, Any]) -> None:
        entry.setdefault("timestamp", time.time())
        snap = _snapshot_dict(self._hot)
        if snap:
            entry["context_version"] = snap.get("context_version", 0)
            entry["current_state"] = snap.get("state", "unknown")
        self._event_log.append(entry)
        if self._session_dir:
            self._event_log.flush_to(self._session_dir / "realtime_events.jsonl")

    def _context_signature(self) -> tuple | None:
        if self._hot is None:
            return None
        mem = self._hot.read()
        snap = mem.get("snapshot")
        if snap is None:
            return None
        trans = mem.get("latest_transition")
        return (
            snap.get("state"), snap.get("status"),
            snap.get("pending_question"),
            trans.get("decision_id") if trans else None,
        )

    def _context_needs_refresh(self) -> bool:
        sig = self._context_signature()
        if sig is None:
            return False
        if self._last_sig is None:
            self._last_sig = sig
            return True
        if sig != self._last_sig:
            self._last_sig = sig
            return True
        return False

    def _build_instructions(self) -> str:
        if self._hot is None:
            return QWEN_SYSTEM_INSTRUCTION
        ctx = self._hot.compact_context()
        return f"{QWEN_SYSTEM_INSTRUCTION}\n\n[TASK_STATE]\n{ctx}"

    def _mic_callback(self, queue: asyncio.Queue[bytes], mic_suppressed: asyncio.Event):
        loop = asyncio.get_event_loop()

        def callback(indata, frames, time_info, status):
            if mic_suppressed.is_set():
                return
            if status:
                self._log({"type": "mic_status", "status": str(status)})

            async def _enqueue():
                try:
                    queue.put_nowait(bytes(indata))
                except asyncio.QueueFull:
                    try:
                        queue.get_nowait()
                        queue.put_nowait(bytes(indata))
                    except (asyncio.QueueEmpty, asyncio.QueueFull):
                        pass

            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_enqueue()))

        return callback

    async def run(self) -> None:
        self._stop.clear()
        self._loop = asyncio.get_running_loop()
        attempt = 0
        while attempt < MAX_CONNECT_ATTEMPTS and not self._stop.is_set():
            attempt += 1
            try:
                await self._connect_and_run()
                return
            except (ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
                self._connected = False
                self._error_count += 1
                self._log({"type": "disconnected", "error": str(exc)})
                if self._stop.is_set():
                    return
                if attempt < MAX_CONNECT_ATTEMPTS:
                    print(f"[qwen] disconnected ({attempt}/{MAX_CONNECT_ATTEMPTS}), "
                          f"reconnecting in {RECONNECT_BACKOFF:.1f}s")
                    await asyncio.sleep(RECONNECT_BACKOFF)
                else:
                    print(f"[qwen] disconnected ({attempt}/{MAX_CONNECT_ATTEMPTS}), giving up")
                    return
            except Exception as exc:
                self._connected = False
                self._error_count += 1
                self._log({"type": "error", "error_type": type(exc).__name__, "error": str(exc)})
                if self._stop.is_set():
                    return
                if attempt < MAX_CONNECT_ATTEMPTS:
                    print(f"[qwen] error ({attempt}/{MAX_CONNECT_ATTEMPTS})")
                    await asyncio.sleep(RECONNECT_BACKOFF)
                else:
                    print(f"[qwen] error ({attempt}/{MAX_CONNECT_ATTEMPTS}), giving up")
                    return
        self._connected = False

    async def _connect_and_run(self) -> None:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with websockets.connect(self._ws_url, additional_headers=headers, close_timeout=2) as ws:
            self._connected = True
            self._log({"type": "connected"})
            print(f"[qwen] connected to {self._model} (voice={self._voice}, vad={self._vad_type})")

            await self._send_session_update(ws)

            mic_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=32)
            spk_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=64)
            mic_suppressed = asyncio.Event()

            mic_stream = sd.RawInputStream(
                samplerate=INPUT_SAMPLE_RATE, blocksize=INPUT_CHUNK,
                channels=1, dtype="int16",
                callback=self._mic_callback(mic_queue, mic_suppressed),
            )
            spk_stream = sd.RawOutputStream(
                samplerate=OUTPUT_SAMPLE_RATE, channels=1, dtype="int16",
            )
            mic_stream.start()
            spk_stream.start()

            async def _suppress_mic():
                mic_suppressed.set()
                _clear_q(mic_queue)
                self._log({"type": "mic_suppressed"})

            async def _resume_mic():
                while not spk_queue.empty():
                    await asyncio.sleep(0.1)
                await asyncio.sleep(MIC_RESUME_COOLDOWN_S)
                mic_suppressed.clear()
                _clear_q(mic_queue)
                self._log({"type": "mic_resumed"})

            async def mic_sender():
                while not self._stop.is_set() and self._connected:
                    try:
                        chunk = await asyncio.wait_for(mic_queue.get(), timeout=0.5)
                        if mic_suppressed.is_set():
                            continue
                        b64 = base64.b64encode(chunk).decode()
                        await ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": b64}))
                    except TimeoutError:
                        continue
                    except Exception:
                        break

            async def spk_player():
                while not self._stop.is_set():
                    try:
                        chunk = await asyncio.wait_for(spk_queue.get(), timeout=0.5)
                        await asyncio.to_thread(spk_stream.write, chunk)
                    except TimeoutError:
                        continue
                    except Exception:
                        break

            async def ctx_pusher():
                while not self._stop.is_set() and self._connected:
                    if self._context_needs_refresh():
                        await ws.send(json.dumps({
                            "type": "session.update",
                            "session": {"instructions": self._build_instructions()},
                        }))
                        self._log({"type": "context_refreshed"})
                    await asyncio.sleep(1.0)

            mic_task = asyncio.create_task(mic_sender(), name="qwen-mic")
            spk_task = asyncio.create_task(spk_player(), name="qwen-spk")
            ctx_task = asyncio.create_task(ctx_pusher(), name="qwen-ctx")

            async def shutdown_watcher():
                await self._stop.wait()
                await ws.close()

            sw_task = asyncio.create_task(shutdown_watcher(), name="qwen-shutdown")

            turn_end_at: float | None = None
            response_in_progress: bool = False
            resume_task: asyncio.Task | None = None
            pending_audio: list[bytes] = []
            pending_transcript: str | None = None
            current_user_transcript: str | None = None
            turn_snapshot: dict[str, Any] | None = None
            turn_plan: dict[str, Any] | None = None

            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    etype = msg.get("type", "")

                    if etype == "input_audio_buffer.speech_started":
                        self._user_turns += 1
                        self._log({"type": "speech_started"})
                        _clear_q(spk_queue)
                        pending_audio.clear()
                        pending_transcript = None
                        if response_in_progress:
                            await ws.send(json.dumps({"type": "response.cancel"}))
                            self._log({"type": "response_cancelled"})
                            response_in_progress = False
                            await _suppress_mic()
                            if resume_task is not None:
                                resume_task.cancel()
                            resume_task = asyncio.create_task(_resume_mic(), name="qwen-resume")

                    elif etype == "input_audio_buffer.speech_stopped":
                        self._log({"type": "speech_stopped"})
                        turn_end_at = time.monotonic()
                        # freeze snapshot at question time
                        turn_snapshot = _snapshot_dict(self._hot)

                    elif etype == "response.created":
                        response_in_progress = True
                        self._log({"type": "response_start"})
                        await _suppress_mic()
                        pending_audio.clear()
                        pending_transcript = None
                        if resume_task is not None:
                            resume_task.cancel()

                    elif etype == "conversation.item.input_audio_transcription.completed":
                        transcript = msg.get("transcript", "")
                        current_user_transcript = transcript
                        self._log({"type": "user_transcript", "transcript": transcript})
                        print(f"\n[user] {transcript}")
                        # generate grounded response plan from frozen snapshot
                        if turn_snapshot is not None:
                            dialogue = self._hot.read().get("recent_dialogue", []) if self._hot else []
                            turn_plan = grounded_plan(
                                snapshot=turn_snapshot,
                                user_transcript=transcript,
                                recent_dialogue=dialogue,
                            )

                    elif etype == "response.audio_transcript.done":
                        transcript = msg.get("transcript", "")
                        pending_transcript = transcript
                        self._assistant_turns += 1
                        self._log({"type": "assistant_transcript", "transcript": transcript})
                        print(f"[qwen] {transcript}")

                    elif etype == "response.audio.delta":
                        audio_b64 = msg.get("delta", "")
                        if audio_b64:
                            raw_pcm = base64.b64decode(audio_b64)
                            pending_audio.append(raw_pcm)
                            if turn_end_at is not None:
                                lat = (time.monotonic() - turn_end_at) * 1000
                                self._first_audio_lats.append(lat)
                                turn_end_at = None

                    elif etype == "conversation.item.input_audio_transcription.delta":
                        pass

                    elif etype == "response.done":
                        response_in_progress = False
                        self._log({"type": "response_end"})

                        # ── fact gate: use frozen snapshot, not latest ──
                        candidate_transcript = pending_transcript or ""
                        plan = turn_plan or {}
                        snap_for_gate = turn_snapshot

                        allowed, override_text = check_fact_gate(
                            assistant_transcript=candidate_transcript,
                            snapshot=snap_for_gate,
                        )

                        delivered_text: str
                        delivery_source: str

                        if allowed:
                            for chunk in pending_audio:
                                await spk_queue.put(chunk)
                            delivered_text = candidate_transcript
                            delivery_source = "qwen"
                            self._log({"type": "fact_gate", "allowed": True})
                            self._log({"type": "delivered_response", "source": "qwen",
                                        "text": delivered_text})
                        else:
                            pending_audio.clear()
                            delivered_text = override_text or "还没有确认。"
                            delivery_source = "grounded_override"
                            self._log({"type": "fact_gate", "allowed": False,
                                        "candidate_transcript": candidate_transcript,
                                        "delivered_text": delivered_text})
                            self._log({"type": "delivered_response", "source": "grounded_override",
                                        "text": delivered_text})
                            print(f"[noma] {delivered_text}")
                            # play override via local TTS if available
                            try:
                                import subprocess, shutil
                                if shutil.which("say"):
                                    await asyncio.to_thread(
                                        subprocess.run, ["say", delivered_text],
                                        capture_output=True, timeout=5,
                                    )
                            except Exception:
                                pass

                        # ── dialogue memory ──
                        if current_user_transcript and candidate_transcript:
                            if self._hot is not None:
                                self._hot.add_dialogue_turn(
                                    user_transcript=current_user_transcript,
                                    candidate_assistant_transcript=candidate_transcript,
                                    delivered_assistant_transcript=delivered_text,
                                    delivery_source=delivery_source,
                                    response_was_grounded=allowed,
                                    state_at_question=(
                                        snap_for_gate.get("state") if snap_for_gate else None
                                    ),
                                    session_dir=self._session_dir,
                                )

                        current_user_transcript = None
                        turn_snapshot = None
                        turn_plan = None
                        pending_audio.clear()
                        pending_transcript = None

                        if resume_task is not None:
                            resume_task.cancel()
                        resume_task = asyncio.create_task(_resume_mic(), name="qwen-resume")

                    elif etype == "error":
                        self._error_count += 1
                        err = msg.get("error", {})
                        self._log({"type": "api_error", "error": err.get("message", str(msg))})

                    elif etype == "session.updated":
                        self._log({"type": "session_updated"})

            finally:
                self._connected = False
                if resume_task is not None:
                    resume_task.cancel()
                sw_task.cancel()
                for t in (ctx_task, mic_task, spk_task, sw_task, resume_task):
                    if t is not None:
                        t.cancel()
                for t in (ctx_task, mic_task, spk_task, sw_task, resume_task):
                    if t is not None:
                        with contextlib.suppress(asyncio.CancelledError):
                            await t
                mic_stream.stop()
                mic_stream.close()
                spk_stream.stop()
                spk_stream.close()

    async def _send_session_update(self, ws) -> None:
        await ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "voice": self._voice,
                "input_audio_format": "pcm",
                "output_audio_format": "pcm",
                "instructions": self._build_instructions(),
                "input_audio_transcription": {
                    "model": "qwen3-asr-flash-realtime",
                },
                "turn_detection": {
                    "type": self._vad_type,
                    "threshold": 0.5,
                    "silence_duration_ms": 800,
                },
            },
        }))
        self._last_sig = self._context_signature()
