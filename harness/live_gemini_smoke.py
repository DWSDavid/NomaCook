"""Gemini Live smoke test: Mac webcam + mic in, spoken scene description out.

Track B (runbook Step B1). Validates the five external risks before Step 4:
API key, Live model name, video frame input, audio in/out, barge-in.

This is a pipeline smoke test, NOT the production shape — production uses
audio-only Live + separate VLM (spec §11.2); audio+video Live burns context
in ~2 minutes without compression, so default --duration stays short.

Usage:
    .venv/bin/python harness/live_gemini_smoke.py                 # audio+video
    .venv/bin/python harness/live_gemini_smoke.py --no-video      # audio only
    .venv/bin/python harness/live_gemini_smoke.py --half-duplex   # no headphones
    .venv/bin/python harness/live_gemini_smoke.py --kickoff "你现在看到什么?"

Requires: `uv pip install -p .venv sounddevice` and GEMINI_API_KEY in .env.
Wear headphones unless using --half-duplex (speaker audio re-enters the mic
and the model talks to itself).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2

from google import genai
from google.genai import types

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSIONS_DIR = REPO_ROOT / "data" / "sessions"

DEFAULT_MODEL = "gemini-3.1-flash-live-preview"

# Live API audio contract: 16 kHz mono PCM in, 24 kHz mono PCM out.
SEND_RATE = 16_000
RECV_RATE = 24_000
CHUNK_MS = 50  # 20-100 ms per official guidance

SYSTEM_INSTRUCTION = (
    "你是 NomaChef(诺妈)的厨房场景描述员。你通过摄像头看到用户的第一人称画面。"
    "用户问你看到什么时,用简洁的中文口语描述画面里的物体、位置和正在发生的事,"
    "重点关注厨房相关物品(锅、碗、瓶、食材、手部动作)。"
    "回答保持在三句话以内。听不清或看不清就直说,不要编造。"
)


def load_env(path: Path) -> None:
    """Minimal .env loader — no python-dotenv dependency."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


class AuditLog:
    """Append-only JSONL audit of everything that crossed the wire."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a")
        self.path = path

    def write(self, event: str, **payload) -> None:
        row = {"t": time.time(), "event": event, **payload}
        self._fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


class SpeakerBuffer:
    """Thread-safe byte buffer feeding the sounddevice output callback.

    clear() implements barge-in: when the server says `interrupted`, whatever
    audio is still queued must be dropped immediately.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._lock = threading.Lock()

    def extend(self, data: bytes) -> None:
        with self._lock:
            self._buf.extend(data)

    def take(self, nbytes: int) -> bytes:
        with self._lock:
            out = bytes(self._buf[:nbytes])
            del self._buf[: len(out)]
        return out

    def clear(self) -> int:
        with self._lock:
            dropped = len(self._buf)
            self._buf.clear()
        return dropped

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._buf)


async def mic_task(
    session, speaker: SpeakerBuffer, log: AuditLog, half_duplex: bool
) -> None:
    import sounddevice as sd

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=64)
    blocksize = SEND_RATE * CHUNK_MS // 1000

    def callback(indata, frames, t, status) -> None:
        if status:
            loop.call_soon_threadsafe(log.write, "mic_status", status=str(status))
        chunk = bytes(indata)
        try:
            loop.call_soon_threadsafe(queue.put_nowait, chunk)
        except RuntimeError:
            pass  # loop closed during shutdown

    stream = sd.RawInputStream(
        samplerate=SEND_RATE,
        blocksize=blocksize,
        channels=1,
        dtype="int16",
        callback=callback,
    )
    with stream:
        log.write("mic_open", rate=SEND_RATE, chunk_ms=CHUNK_MS)
        while True:
            chunk = await queue.get()
            # Half-duplex: drop mic input while the model is audibly speaking,
            # so speaker output can't feed back. Kills barge-in by design.
            if half_duplex and speaker.pending > 0:
                continue
            await session.send_realtime_input(
                audio=types.Blob(data=chunk, mime_type=f"audio/pcm;rate={SEND_RATE}")
            )


async def camera_task(
    session, log: AuditLog, source: int, fps: float, max_dim: int
) -> None:
    loop = asyncio.get_running_loop()
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        raise RuntimeError(f"camera source {source} failed to open")
    log.write("camera_open", source=source, fps=fps, max_dim=max_dim)

    def grab_jpeg() -> bytes | None:
        ok, frame = cap.read()
        if not ok:
            return None
        h, w = frame.shape[:2]
        scale = max_dim / max(h, w)
        if scale < 1.0:
            frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
        ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return jpeg.tobytes() if ok else None

    try:
        sent = 0
        while True:
            jpeg = await loop.run_in_executor(None, grab_jpeg)
            if jpeg is None:
                log.write("camera_read_failed")
                await asyncio.sleep(1.0)
                continue
            await session.send_realtime_input(
                video=types.Blob(data=jpeg, mime_type="image/jpeg")
            )
            sent += 1
            if sent % 10 == 1:
                log.write("frames_sent", count=sent, last_bytes=len(jpeg))
            await asyncio.sleep(1.0 / fps)
    finally:
        cap.release()


async def receive_task(session, speaker: SpeakerBuffer, log: AuditLog) -> None:
    while True:
        async for response in session.receive():
            sc = response.server_content
            if sc is None:
                continue
            if sc.interrupted:
                dropped = speaker.clear()
                log.write("interrupted", dropped_bytes=dropped)
                print("\n[barge-in] 播放中断, 丢弃缓冲", flush=True)
            if sc.input_transcription and sc.input_transcription.text:
                text = sc.input_transcription.text
                log.write("transcript_in", text=text)
                print(f"[你] {text}", flush=True)
            if sc.output_transcription and sc.output_transcription.text:
                text = sc.output_transcription.text
                log.write("transcript_out", text=text)
                print(f"[诺妈] {text}", flush=True)
            if sc.model_turn:
                for part in sc.model_turn.parts or []:
                    if part.inline_data and part.inline_data.data:
                        speaker.extend(part.inline_data.data)
            if sc.turn_complete:
                log.write("turn_complete")


async def run(args: argparse.Namespace) -> None:
    import sounddevice as sd

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("GEMINI_API_KEY 未设置。按 docs/SETUP-KEYS.md 填入 .env 后重试。")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    log = AuditLog(SESSIONS_DIR / f"{stamp}_gemini_live_smoke.jsonl")
    log.write(
        "session_start",
        model=args.model,
        video=not args.no_video,
        fps=args.fps,
        half_duplex=args.half_duplex,
        duration_s=args.duration,
    )

    client = genai.Client(api_key=api_key)
    config = types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        system_instruction=SYSTEM_INSTRUCTION,
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )

    speaker = SpeakerBuffer()

    def out_callback(outdata, frames, t, status) -> None:
        nbytes = len(outdata)
        chunk = speaker.take(nbytes)
        outdata[: len(chunk)] = chunk
        if len(chunk) < nbytes:
            outdata[len(chunk) :] = b"\x00" * (nbytes - len(chunk))

    out_stream = sd.RawOutputStream(
        samplerate=RECV_RATE, channels=1, dtype="int16", callback=out_callback
    )

    print(f"连接 {args.model} …(Ctrl-C 退出, 上限 {args.duration}s)")
    try:
        async with client.aio.live.connect(model=args.model, config=config) as session:
            log.write("connected")
            print("已连接。开始说话吧。", "" if args.no_video else "摄像头帧同步发送中。")
            if args.kickoff:
                await session.send_client_content(
                    turns=types.Content(
                        role="user", parts=[types.Part(text=args.kickoff)]
                    ),
                    turn_complete=True,
                )
                log.write("kickoff_sent", text=args.kickoff)

            with out_stream:
                tasks = [
                    asyncio.create_task(receive_task(session, speaker, log)),
                    asyncio.create_task(
                        mic_task(session, speaker, log, args.half_duplex)
                    ),
                ]
                if not args.no_video:
                    tasks.append(
                        asyncio.create_task(
                            camera_task(
                                session, log, args.source, args.fps, args.max_dim
                            )
                        )
                    )
                done, pending = await asyncio.wait(
                    tasks,
                    timeout=args.duration,
                    return_when=asyncio.FIRST_EXCEPTION,
                )
                for task in pending:
                    task.cancel()
                for task in done:
                    if task.exception():
                        raise task.exception()
                log.write("duration_reached")
                print(f"\n达到 {args.duration}s 上限, 正常收尾。")
    except KeyboardInterrupt:
        log.write("keyboard_interrupt")
        print("\n手动退出。")
    except Exception as exc:  # noqa: BLE001 — smoke test: log then re-raise
        log.write("error", type=type(exc).__name__, message=str(exc))
        raise
    finally:
        log.write("session_end")
        log.close()
        print(f"审计日志: {log.path}")


def main() -> None:
    load_env(REPO_ROOT / ".env")  # before argparse: GEMINI_LIVE_MODEL feeds a default
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--model",
        default=os.environ.get("GEMINI_LIVE_MODEL", DEFAULT_MODEL),
        help="Live 模型名 (env GEMINI_LIVE_MODEL 可覆盖)",
    )
    parser.add_argument("--source", type=int, default=0, help="摄像头编号 (默认 0)")
    parser.add_argument("--fps", type=float, default=1.0, help="视频帧率 (默认 1)")
    parser.add_argument("--max-dim", type=int, default=768, help="帧最长边像素")
    parser.add_argument("--duration", type=float, default=120, help="会话时长上限秒")
    parser.add_argument("--no-video", action="store_true", help="纯音频模式")
    parser.add_argument(
        "--half-duplex",
        action="store_true",
        help="播放时静音麦克风 (不戴耳机用; 会禁用打断)",
    )
    parser.add_argument(
        "--kickoff", default=None, help="连接后先发一句文字, 如 '你现在看到什么?'"
    )
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
