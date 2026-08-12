"""Qwen3.5 Omni Realtime adapter via native WebSocket.

Half-duplex: mic muted during assistant playback to prevent acoustic echo.
Context injection: session.update with merged instructions.
Reconnect: bounded (initial + 1 retry).
Shutdown: close_timeout=2 on WebSocket + bounded thread join.
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

INPUT_SAMPLE_RATE = 16000
OUTPUT_SAMPLE_RATE = 24000
CHUNK_MS = 100
INPUT_CHUNK = int(INPUT_SAMPLE_RATE * CHUNK_MS / 1000)
MAX_CONNECT_ATTEMPTS = 2
RECONNECT_BACKOFF = 2.0
MIC_RESUME_COOLDOWN_S = 0.5

QWEN_SYSTEM_INSTRUCTION = """你是 NomaCook 的任务辅助者，不能闲聊。
你无法看到原始画面，只能依据 [TASK_STATE] 中的信息回答。
不要声称你能看到什么、你在检查什么或帮你打开任何东西。
不要虚构视觉观察或物理动作。

回答规则：
- 用户问"做到哪一步了"时，只回答当前步骤标题和下一步该做什么。
  例如："我检测到你正在{current_step_title}。下一步，{current_instruction前半段}。"
- ON_TRACK 时不主动讲话。
- UNCERTAIN 时只问一次 pending_question，不要重复问。
- COMPLETE 时简短宣布一次。
- 不得反问"你想检查哪一层""你想聊什么"。
- 不得复述用户整句话。
- 每次最多两句话。
- 不得进入开放式闲聊。"""


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
            f"Missing environment variables: {', '.join(missing)}. "
            f"Set them before running with --qwen-realtime."
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


class QwenRealtimeAdapter:

    def __init__(
        self,
        *,
        model: str = "qwen3.5-omni-flash-realtime",
        voice: str = "Tina",
        url_override: str | None = None,
        hot_memory=None,
        session_dir: Path | None = None,
    ):
        _check_env()
        self._model = model
        self._voice = voice
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
        if self._hot is not None:
            mem = self._hot.read()
            snap = mem.get("snapshot")
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
            snap.get("state"),
            snap.get("status"),
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
                    print(f"[qwen] disconnected (attempt {attempt}/{MAX_CONNECT_ATTEMPTS}), "
                          f"reconnecting in {RECONNECT_BACKOFF:.1f}s: {exc}")
                    await asyncio.sleep(RECONNECT_BACKOFF)
                else:
                    print(f"[qwen] disconnected (attempt {attempt}/{MAX_CONNECT_ATTEMPTS}), giving up: {exc}")
                    return
            except Exception as exc:
                self._connected = False
                self._error_count += 1
                self._log({"type": "error", "error_type": type(exc).__name__, "error": str(exc)})
                if self._stop.is_set():
                    return
                if attempt < MAX_CONNECT_ATTEMPTS:
                    print(f"[qwen] error (attempt {attempt}/{MAX_CONNECT_ATTEMPTS}), "
                          f"reconnecting in {RECONNECT_BACKOFF:.1f}s: {exc}")
                    await asyncio.sleep(RECONNECT_BACKOFF)
                else:
                    print(f"[qwen] error (attempt {attempt}/{MAX_CONNECT_ATTEMPTS}), giving up: {exc}")
                    return
        self._connected = False

    async def _connect_and_run(self) -> None:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with websockets.connect(self._ws_url, additional_headers=headers, close_timeout=2) as ws:
            self._connected = True
            self._log({"type": "connected"})
            print(f"[qwen] connected to {self._model}")

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
                self._log({"type": "mic_suppressed"})

            async def _resume_mic():
                # ponytail: wait for speaker queue to drain + cooldown
                while not spk_queue.empty():
                    await asyncio.sleep(0.1)
                await asyncio.sleep(MIC_RESUME_COOLDOWN_S)
                mic_suppressed.clear()
                self._log({"type": "mic_resumed"})

            async def mic_sender():
                while not self._stop.is_set() and self._connected:
                    try:
                        chunk = await asyncio.wait_for(mic_queue.get(), timeout=0.5)
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

            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    etype = msg.get("type", "")

                    if etype == "input_audio_buffer.speech_started":
                        self._user_turns += 1
                        self._log({"type": "speech_started"})
                        _clear_q(spk_queue)
                        if response_in_progress:
                            await ws.send(json.dumps({"type": "response.cancel"}))
                            self._log({"type": "response_cancelled"})
                            response_in_progress = False
                            # suppress mic while cancelling old response + cooldown
                            await _suppress_mic()
                            if resume_task is not None:
                                resume_task.cancel()
                            resume_task = asyncio.create_task(_resume_mic(), name="qwen-resume")

                    elif etype == "input_audio_buffer.speech_stopped":
                        self._log({"type": "speech_stopped"})
                        turn_end_at = time.monotonic()

                    elif etype == "response.created":
                        response_in_progress = True
                        self._log({"type": "response_start"})
                        await _suppress_mic()
                        if resume_task is not None:
                            resume_task.cancel()

                    elif etype == "conversation.item.input_audio_transcription.completed":
                        transcript = msg.get("transcript", "")
                        self._log({"type": "user_transcript", "transcript": transcript})
                        print(f"\n[user] {transcript}")

                    elif etype == "response.audio_transcript.done":
                        transcript = msg.get("transcript", "")
                        self._log({"type": "assistant_transcript", "transcript": transcript})
                        self._assistant_turns += 1
                        print(f"[qwen] {transcript}")

                    elif etype == "response.audio.delta":
                        audio_b64 = msg.get("delta", "")
                        if audio_b64:
                            if turn_end_at is not None:
                                lat = (time.monotonic() - turn_end_at) * 1000
                                self._first_audio_lats.append(lat)
                                turn_end_at = None
                            await spk_queue.put(base64.b64decode(audio_b64))

                    elif etype == "conversation.item.input_audio_transcription.delta":
                        pass  # ponytail: only log completed to avoid duplicate prints

                    elif etype == "response.done":
                        response_in_progress = False
                        self._log({"type": "response_end"})
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
                "turn_detection": {
                    "type": "semantic_vad",
                    "threshold": 0.5,
                    "silence_duration_ms": 800,
                },
            },
        }))
        self._last_sig = self._context_signature()
