"""Qwen3.5 Omni Realtime adapter via native WebSocket.

Connects to Aliyun Model Studio Realtime API, streams microphone audio,
plays model audio responses, and reads HotMemory for task context injection.

Protocol: https://help.aliyun.com/en/model-studio/realtime
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
INPUT_CHUNK = int(INPUT_SAMPLE_RATE * CHUNK_MS / 1000)  # 1600 samples
RECONNECT_BACKOFF = 2.0
MAX_RECONNECT_BACKOFF = 30.0


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


QWEN_SYSTEM_INSTRUCTION = """你是 NomaCook 的实时任务助手。
任务真相只来自 Noma StateEngine 提供的 current_task_state。
不要根据猜测宣布步骤完成。
不要自行修改当前步骤。
优先用简短自然的中文回应，每次 1–2 句话。
如果状态为 ON_TRACK，提供简短鼓励或下一步。
如果状态为 UNCERTAIN，只说明缺少什么证据或询问一个问题。
如果状态为 COMPLETE，明确告诉用户任务完成。
不要持续说话，不要每个视觉事件都播报。"""


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
        self._last_context_version: int = 0

        self._event_log = RealtimeEventLog()
        if session_dir:
            (session_dir / "realtime_events.jsonl").touch()

    # ── public read-only ──

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
        self._stop.set()

    # ── logging ──

    def _log(self, entry: dict[str, Any]) -> None:
        self._event_log.append(entry)
        if self._session_dir:
            self._event_log.flush_to(self._session_dir / "realtime_events.jsonl")

    # ── context refresh ──

    def _check_context_refresh(self) -> str | None:
        if self._hot is None:
            return None
        memory = self._hot.read()
        cv = memory.get("context_version", 0)
        if cv == self._last_context_version:
            return None
        self._last_context_version = cv
        snap = memory.get("snapshot")
        if snap is None:
            return None
        status = snap.get("status", "ON_TRACK")
        if status in ("UNCERTAIN", "DEVIATING", "COMPLETE"):
            return self._hot.compact_context()
        if memory.get("pending_question"):
            return self._hot.compact_context()
        if memory.get("latest_transition"):
            return self._hot.compact_context()
        return None

    # ── mic callback ──

    def _mic_callback(self, queue: asyncio.Queue[bytes]):
        loop = asyncio.get_event_loop()

        def callback(indata, frames, time_info, status):
            if status:
                self._log({"type": "mic_status", "status": str(status), "timestamp": time.time()})
            try:
                loop.call_soon_threadsafe(queue.put_nowait, bytes(indata))
            except asyncio.QueueFull:
                pass

        return callback

    # ── main loop ──

    async def run(self) -> None:
        self._stop.clear()
        backoff = RECONNECT_BACKOFF
        while not self._stop.is_set():
            try:
                await self._connect_and_run()
                break
            except (ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
                self._connected = False
                self._error_count += 1
                self._log({"type": "disconnected", "error": str(exc), "timestamp": time.time()})
                if self._stop.is_set():
                    break
                print(f"[qwen] disconnected, reconnecting in {backoff:.1f}s: {exc}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_RECONNECT_BACKOFF)
            except Exception as exc:
                self._connected = False
                self._error_count += 1
                self._log({"type": "error", "error_type": type(exc).__name__,
                           "error": str(exc), "timestamp": time.time()})
                if self._stop.is_set():
                    break
                print(f"[qwen] error, reconnecting in {backoff:.1f}s: {exc}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_RECONNECT_BACKOFF)
        self._connected = False

    async def _connect_and_run(self) -> None:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with websockets.connect(self._ws_url, additional_headers=headers) as ws:
            self._connected = True
            self._log({"type": "connected", "model": self._model,
                       "voice": self._voice, "timestamp": time.time()})
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
                    ctx = self._check_context_refresh()
                    if ctx:
                        await ws.send(json.dumps({
                            "type": "conversation.item.create",
                            "item": {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": f"[TASK_STATE]\n{ctx}"}],
                            },
                        }))
                        await asyncio.sleep(0.5)
                    await asyncio.sleep(1.0)

            mic_task = asyncio.create_task(mic_sender(), name="qwen-mic")
            spk_task = asyncio.create_task(spk_player(), name="qwen-spk")
            ctx_task = asyncio.create_task(ctx_pusher(), name="qwen-ctx")

            response_start: float | None = None

            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    etype = msg.get("type", "")

                    if etype == "input_audio_buffer.speech_started":
                        self._user_turns += 1
                        self._log({"type": "speech_started", "timestamp": time.time()})
                        _clear_q(spk_queue)
                        response_start = time.monotonic()

                    elif etype == "input_audio_buffer.speech_stopped":
                        self._log({"type": "speech_stopped", "timestamp": time.time()})

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
                            if response_start is not None:
                                lat = (time.monotonic() - response_start) * 1000
                                self._first_audio_lats.append(lat)
                                response_start = None
                            await spk_queue.put(base64.b64decode(audio_b64))

                    elif etype == "conversation.item.input_audio_transcription.delta":
                        text = msg.get("text", "")
                        stash = msg.get("stash", "")
                        if text or stash:
                            print(f"\r[user] {text}{stash}", end="", flush=True)

                    elif etype == "response.done":
                        self._log({"type": "response_end", "timestamp": time.time()})

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
        await ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "voice": self._voice,
                "input_audio_format": "pcm",
                "output_audio_format": "pcm",
                "instructions": QWEN_SYSTEM_INSTRUCTION,
                "turn_detection": {
                    "type": "semantic_vad",
                    "threshold": 0.5,
                    "silence_duration_ms": 800,
                },
            },
        }))
