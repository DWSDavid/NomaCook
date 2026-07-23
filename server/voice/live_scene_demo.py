"""Local Gemini Live camera + microphone + speaker integration smoke test."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from functools import partial
import os
from pathlib import Path
import signal
import sys
from typing import Any

import cv2
from dotenv import dotenv_values
from google import genai
from google.genai import types
import sounddevice as sd


DEFAULT_MODEL = "gemini-3.1-flash-live-preview"
INPUT_SAMPLE_RATE = 16_000
OUTPUT_SAMPLE_RATE = 24_000
DEFAULT_FRAME_INTERVAL = 1.0

SYSTEM_INSTRUCTION = """
你是 NomaChef 的实时厨房视觉与语音助手。始终用简洁、自然的中文回答。
你会持续收到用户摄像头的低帧率画面和麦克风音频。回答视觉问题时，只描述画面中
确实可见的内容，优先关注厨房相关的物体、容器、食材、手部动作、灶台状态和明显的
安全风险；无法确认时明确说不确定，不要臆测步骤已经完成。默认回答控制在 2 至 4 句，
用户要求详细说明时再展开。不要仅因收到新画面就不断主动说话；等待用户提问，或响应
启动时的一次场景描述请求。
""".strip()

DEFAULT_INITIAL_PROMPT = (
    "请根据你目前收到的摄像头画面，用中文描述眼前场景。先说整体环境，再列出你能"
    "确认的厨房相关物体或手部动作；看不清或不是厨房场景也请如实说明。"
)


def _api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key and Path(".env").is_file():
        key = str(dotenv_values(".env").get("GEMINI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY 未配置。请把已有 key 写入仓库根目录的本地 .env；"
            "不要把 key 提交到 Git 或粘贴到聊天中。"
        )
    return key


def _camera_source(value: str) -> int | str:
    return int(value) if value.isdigit() else value


def _open_camera(source: int | str) -> cv2.VideoCapture:
    backend = (
        cv2.CAP_AVFOUNDATION
        if sys.platform == "darwin" and isinstance(source, int)
        else cv2.CAP_ANY
    )
    capture = cv2.VideoCapture(source, backend)
    if not capture.isOpened():
        capture.release()
        raise RuntimeError(f"无法打开摄像头或视频源：{source}")
    return capture


def _resize_for_live(frame: Any, max_width: int) -> Any:
    height, width = frame.shape[:2]
    if width <= max_width:
        return frame
    scale = max_width / width
    return cv2.resize(
        frame,
        (max_width, max(1, round(height * scale))),
        interpolation=cv2.INTER_AREA,
    )


def _clear_queue(queue: asyncio.Queue[bytes]) -> None:
    with contextlib.suppress(asyncio.QueueEmpty):
        while True:
            queue.get_nowait()


async def _send_camera(
    session: Any,
    capture: cv2.VideoCapture,
    stop: asyncio.Event,
    first_frame_sent: asyncio.Event,
    *,
    frame_interval: float,
    frame_width: int,
    display: bool,
) -> None:
    loop = asyncio.get_running_loop()
    next_send = 0.0

    while not stop.is_set():
        ok, frame = await asyncio.to_thread(capture.read)
        if not ok:
            print("摄像头读取失败，正在结束 session。", file=sys.stderr)
            stop.set()
            return

        now = loop.time()
        if now >= next_send:
            live_frame = _resize_for_live(frame, frame_width)
            encoded_ok, encoded = cv2.imencode(
                ".jpg", live_frame, [cv2.IMWRITE_JPEG_QUALITY, 75]
            )
            if encoded_ok:
                await session.send_realtime_input(
                    video=types.Blob(
                        data=encoded.tobytes(), mime_type="image/jpeg"
                    )
                )
                first_frame_sent.set()
                next_send = now + frame_interval

        if display:
            cv2.putText(
                frame,
                "Gemini Live: q to stop",
                (20, 40),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (0, 220, 0),
                2,
                cv2.LINE_AA,
            )
            cv2.imshow("NomaChef Gemini Live", frame)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                stop.set()
                return

        await asyncio.sleep(0.01 if display else 0.03)


async def _send_microphone(
    session: Any, input_queue: asyncio.Queue[bytes], stop: asyncio.Event
) -> None:
    while not stop.is_set():
        try:
            chunk = await asyncio.wait_for(input_queue.get(), timeout=0.25)
        except TimeoutError:
            continue
        await session.send_realtime_input(
            audio=types.Blob(
                data=chunk, mime_type=f"audio/pcm;rate={INPUT_SAMPLE_RATE}"
            )
        )


async def _play_audio(
    output_stream: sd.RawOutputStream,
    output_queue: asyncio.Queue[bytes],
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        try:
            chunk = await asyncio.wait_for(output_queue.get(), timeout=0.25)
        except TimeoutError:
            continue
        await asyncio.to_thread(output_stream.write, chunk)


async def _receive_model(
    session: Any,
    output_queue: asyncio.Queue[bytes] | None,
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        async for response in session.receive():
            content = response.server_content
            if content is None:
                continue

            if content.interrupted and output_queue is not None:
                _clear_queue(output_queue)
                print("\n[Gemini 输出被打断]", flush=True)

            if content.input_transcription and content.input_transcription.text:
                print(
                    f"\n你：{content.input_transcription.text}",
                    end="",
                    flush=True,
                )

            if content.output_transcription and content.output_transcription.text:
                print(
                    f"\nGemini：{content.output_transcription.text}",
                    end="",
                    flush=True,
                )

            turn = content.model_turn
            if turn is None:
                continue
            for part in turn.parts or []:
                inline = part.inline_data
                if (
                    output_queue is not None
                    and inline is not None
                    and inline.data
                    and (inline.mime_type or "").startswith("audio/")
                ):
                    await output_queue.put(inline.data)


async def _send_initial_prompt(
    session: Any,
    first_frame_sent: asyncio.Event,
    stop: asyncio.Event,
    prompt: str,
) -> None:
    await first_frame_sent.wait()
    if stop.is_set() or not prompt:
        return
    await asyncio.sleep(1.2)
    await session.send_realtime_input(text=prompt)


def _audio_callback(
    loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[bytes]
):
    def callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
        del frames, time_info
        if status:
            loop.call_soon_threadsafe(
                partial(print, f"\n[麦克风状态] {status}", file=sys.stderr)
            )
        chunk = bytes(indata)

        def enqueue() -> None:
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(chunk)

        loop.call_soon_threadsafe(enqueue)

    return callback


async def run(args: argparse.Namespace) -> None:
    api_key = _api_key()
    source = _camera_source(args.source)
    capture = _open_camera(source)
    stop = asyncio.Event()
    first_frame_sent = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    input_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=12)
    output_queue: asyncio.Queue[bytes] | None = (
        None if args.no_speaker else asyncio.Queue(maxsize=48)
    )

    input_stream: sd.RawInputStream | None = None
    output_stream: sd.RawOutputStream | None = None

    config = types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        system_instruction=SYSTEM_INSTRUCTION,
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
    )
    client = genai.Client(api_key=api_key)

    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(
        f"连接 {args.model}；视频源={args.source}，分辨率={width}x{height}，"
        f"发送间隔={args.frame_interval:.1f}s"
    )
    print("请优先使用耳机避免扬声器回灌；按 q 或 Ctrl-C 结束。")

    tasks: list[asyncio.Task[Any]] = []
    try:
        async with client.aio.live.connect(
            model=args.model, config=config
        ) as session:
            if not args.no_microphone:
                input_stream = sd.RawInputStream(
                    samplerate=INPUT_SAMPLE_RATE,
                    blocksize=1_600,
                    device=args.input_device,
                    channels=1,
                    dtype="int16",
                    callback=_audio_callback(loop, input_queue),
                )
                input_stream.start()

            if output_queue is not None:
                output_stream = sd.RawOutputStream(
                    samplerate=OUTPUT_SAMPLE_RATE,
                    device=args.output_device,
                    channels=1,
                    dtype="int16",
                )
                output_stream.start()

            tasks.extend(
                [
                    asyncio.create_task(
                        _send_camera(
                            session,
                            capture,
                            stop,
                            first_frame_sent,
                            frame_interval=args.frame_interval,
                            frame_width=args.frame_width,
                            display=not args.no_display,
                        ),
                        name="camera",
                    ),
                    asyncio.create_task(
                        _receive_model(session, output_queue, stop),
                        name="receive",
                    ),
                    asyncio.create_task(
                        _send_initial_prompt(
                            session,
                            first_frame_sent,
                            stop,
                            args.initial_prompt,
                        ),
                        name="initial-prompt",
                    ),
                ]
            )
            if input_stream is not None:
                tasks.append(
                    asyncio.create_task(
                        _send_microphone(session, input_queue, stop),
                        name="microphone",
                    )
                )
            if output_stream is not None and output_queue is not None:
                tasks.append(
                    asyncio.create_task(
                        _play_audio(output_stream, output_queue, stop),
                        name="speaker",
                    )
                )

            try:
                if args.duration > 0:
                    await asyncio.wait_for(stop.wait(), timeout=args.duration)
                else:
                    await stop.wait()
            except TimeoutError:
                print(f"\n达到 {args.duration:.0f}s smoke 时长，正在结束。")
                stop.set()
            finally:
                if input_stream is not None:
                    with contextlib.suppress(Exception):
                        await session.send_realtime_input(audio_stream_end=True)
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        for stream in (input_stream, output_stream):
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.stop()
                stream.close()
        capture.release()
        cv2.destroyAllWindows()
        client.close()
        print("\nGemini Live session 已关闭。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="NomaChef 本地摄像头 + 麦克风 Gemini Live 整合测试"
    )
    parser.add_argument("--source", default="0", help="摄像头序号或视频路径")
    parser.add_argument("--model", default=os.getenv("GEMINI_LIVE_MODEL", DEFAULT_MODEL))
    parser.add_argument(
        "--duration",
        type=float,
        default=100.0,
        help="运行秒数；0 表示运行到手动退出（音频+视频单连接官方限制约 2 分钟）",
    )
    parser.add_argument(
        "--frame-interval",
        type=float,
        default=DEFAULT_FRAME_INTERVAL,
        help="发送视频帧的秒间隔，官方上限为每秒 1 帧",
    )
    parser.add_argument("--frame-width", type=int, default=768)
    parser.add_argument("--initial-prompt", default=DEFAULT_INITIAL_PROMPT)
    parser.add_argument("--input-device", type=int, default=None)
    parser.add_argument("--output-device", type=int, default=None)
    parser.add_argument("--no-display", action="store_true")
    parser.add_argument("--no-microphone", action="store_true")
    parser.add_argument("--no-speaker", action="store_true")
    parser.add_argument("--list-audio-devices", action="store_true")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.list_audio_devices:
        print(sd.query_devices())
        return
    if args.frame_interval < 1.0:
        parser.error("--frame-interval 不能小于 1.0 秒（Gemini Live 视频上限 1 FPS）")
    if args.frame_width < 160:
        parser.error("--frame-width 不能小于 160")
    if args.duration < 0:
        parser.error("--duration 不能为负数")

    try:
        asyncio.run(run(args))
    except RuntimeError as exc:
        parser.exit(2, f"错误：{exc}\n")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
