"""Qwen3.5 Omni Realtime adapter via native WebSocket.

Context injection: session.update with merged instructions.
Interruption: response.cancel via official client event.
Reconnect: bounded (initial + 1 retry).
Shutdown: loop.call_soon_threadsafe + bounded thread join.
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

QWEN_SYSTEM_INSTRUCTION = """你是 NomaCook 的实时任务助手。
任务真相只来自 Noma StateEngine 提供的 current_task_state。
不要根据猜测宣布步骤完成。
不要自行修改当前步骤。
优先用简短自然的中文回应，每次 1–2 句话。
如果状态为 ON_TRACK，提供简短鼓励或下一步。
如果状态为 UNCERTAIN，只说明缺少什么证据或询问一个问题。
如果状态为 COMPLETE，明确告诉用户任务完成。
不要持续说话，不要每个视觉事件都播报。"""


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

        # for shutdown
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
            "qwen_first_audio_mean_ms": sum(lats) / len(lats) if lats else 0,
            "qwen_first_audio_p95_ms": self._p95(lats),
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

    # ── context signature (only meaningful changes trigger update) ──

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
        base = QWEN_SYSTEM_INSTRUCTION
        if self._hot is None:
            return base
        ctx = self._hot.compact_context()
        return f"{base}\n\n[TASK_STATE]\n{ctx}"

    # ── mic callback (thread-safe enqueue, no QueueFull crash) ──

    def _mic_callback(self, queue: asyncio.Queue[bytes]):
        loop = asyncio.get_event_loop()

        def callback(indata, frames, time_info, status):
            if status:
                self._log({"type": "mic_status", "status": str(status)})

            async def _enqueue():
                try:
                    queue.put_nowait(bytes(indata))
                except asyncio.QueueFull:
                    # ponytail: drop oldest to make room
                    try:
                        queue.get_nowait()
                        queue.put_nowait(bytes(indata))
                    except (asyncio.QueueEmpty, asyncio.QueueFull):
                        pass

            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_enqueue()))

        return callback

    # ── main loop ──

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
                    print(f"[qwen] disconnected (attempt {attempt}/{MAX_CONNECT_ATTEMPTS}), reconnecting in {RECONNECT_BACKOFF:.1f}s: {exc}")
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
                    print(f"[qwen] error (attempt {attempt}/{MAX_CONNECT_ATTEMPTS}), reconnecting in {RECONNECT_BACKOFF:.1f}s: {exc}")
                    await asyncio.sleep(RECONNECT_BACKOFF)
                else:
                    print(f"[qwen] error (attempt {attempt}/{MAX_CONNECT_ATTEMPTS}), giving up: {exc}")
                    return
        self._connected = False

    async def _connect_and_run(self) -> None:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with websockets.connect(self._ws_url, additional_headers=headers) as ws:
            self._connected = True
            self._log({"type": "connected"})
            print(f"[qwen] connected to {self._model}")

            await self._send_session_update(ws)

            mic_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=32)
            spk_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=64)

            mic_stream = sd.RawInputStream(
                samplerate=INPUT_SAMPLE_RATE, blocksize=INPUT_CHUNK,
                channels=1, dtype="int16",
                callback=self._mic_callback(mic_queue),
            )
            spk_stream = sd.RawOutputStream(
                samplerate=OUTPUT_SAMPLE_RATE, channels=1, dtype="int16",
            )
            mic_stream.start()
            spk_stream.start()

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
                        inst = self._build_instructions()
                        await ws.send(json.dumps({
                            "type": "session.update",
                            "session": {
                                "instructions": inst,
                            },
                        }))
                        self._log({"type": "context_refreshed"})
                    await asyncio.sleep(1.0)

            mic_task = asyncio.create_task(mic_sender(), name="qwen-mic")
            spk_task = asyncio.create_task(spk_player(), name="qwen-spk")
            ctx_task = asyncio.create_task(ctx_pusher(), name="qwen-ctx")

            turn_end_at: float | None = None
            response_in_progress: bool = False

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

                    elif etype == "input_audio_buffer.speech_stopped":
                        self._log({"type": "speech_stopped"})
                        turn_end_at = time.monotonic()

                    elif etype == "response.created":
                        response_in_progress = True
                        self._log({"type": "response_start"})

                    elif etype == "conversation.item.input_audio_transcription.completed":
                        self._log({"type": "user_transcript", "transcript": msg.get("transcript", "")})
                        print(f"\n[user] {msg.get('transcript', '')}")

                    elif etype == "response.audio_transcript.done":
                        self._log({"type": "assistant_transcript", "transcript": msg.get("transcript", "")})
                        self._assistant_turns += 1
                        print(f"[qwen] {msg.get('transcript', '')}")

                    elif etype == "response.audio.delta":
                        audio_b64 = msg.get("delta", "")
                        if audio_b64:
                            if turn_end_at is not None:
                                lat = (time.monotonic() - turn_end_at) * 1000
                                self._first_audio_lats.append(lat)
                                turn_end_at = None
                            await spk_queue.put(base64.b64decode(audio_b64))

                    elif etype == "conversation.item.input_audio_transcription.delta":
                        text = msg.get("text", "")
                        stash = msg.get("stash", "")
                        if text or stash:
                            print(f"\r[user] {text}{stash}", end="", flush=True)

                    elif etype == "response.done":
                        response_in_progress = False
                        self._log({"type": "response_end"})

                    elif etype == "error":
                        self._error_count += 1
                        err = msg.get("error", {})
                        self._log({"type": "api_error", "error": err.get("message", str(msg))})
                        print(f"[qwen] API error: {err.get('message', err)}")

                    elif etype == "session.updated":
                        self._log({"type": "session_updated"})

            finally:
                self._connected = False
                for t in (ctx_task, mic_task, spk_task):
                    t.cancel()
                for t in (ctx_task, mic_task, spk_task):
                    with contextlib.suppress(asyncio.CancelledError):
                        await t
                mic_stream.stop()
                mic_stream.close()
                spk_stream.stop()
                spk_stream.close()

    async def _send_session_update(self, ws) -> None:
        inst = self._build_instructions()
        await ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "voice": self._voice,
                "input_audio_format": "pcm",
                "output_audio_format": "pcm",
                "instructions": inst,
                "turn_detection": {
                    "type": "semantic_vad",
                    "threshold": 0.5,
                    "silence_duration_ms": 800,
                },
            },
        }))
        self._last_sig = self._context_signature()
