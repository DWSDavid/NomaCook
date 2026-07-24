#!/usr/bin/env python3
"""NomaCook real-time recognition with local or iFLYTEK streaming speech."""

from __future__ import annotations

import argparse
import concurrent.futures
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import cv2
import numpy as np
import torch

from perception.detector import Detection, ObjectDetector
from perception.realtime_recognition import (
    DishConfirmationGate,
    SpeechAnnouncer,
    StableRecognizer,
    catalog_for_profile,
    class_for_prompt,
    labels_zh,
)
from server.gemini_config import gemini_is_configured
from server.vlm.live_dish import LiveDishGuess, identify_live_dish


WINDOW_TITLE = "NomaCook Real-time Recognition"
_FONT_PATHS = (
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
)


def _device(name: str) -> str:
    if name != "auto":
        return name
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _source(value: str) -> int | str:
    return int(value) if value.isdigit() else value


def _audio_device(value: str | None) -> int | str | None:
    if value is None or not value.strip():
        return None
    return int(value) if value.isdigit() else value


def _open_capture(source: int | str) -> cv2.VideoCapture:
    backend = (
        cv2.CAP_AVFOUNDATION
        if sys.platform == "darwin" and isinstance(source, int)
        else cv2.CAP_ANY
    )
    return cv2.VideoCapture(source, backend)


def _best_by_concept(detections: list[Detection]) -> dict[str, Detection]:
    best: dict[str, Detection] = {}
    for detection in detections:
        key = class_for_prompt(detection.label).key
        if key not in best or detection.conf > best[key].conf:
            best[key] = detection
    return best


class _ChineseOverlay:
    def __init__(self) -> None:
        self._pil = None
        self._font = None
        self._small_font = None
        try:
            from PIL import Image, ImageDraw, ImageFont

            font_path = next((p for p in _FONT_PATHS if Path(p).exists()), None)
            if font_path:
                self._pil = (Image, ImageDraw)
                self._font = ImageFont.truetype(font_path, 24)
                self._small_font = ImageFont.truetype(font_path, 18)
        except Exception:
            pass

    def draw(
        self,
        frame: np.ndarray,
        *,
        detections: dict[str, Detection],
        active_keys: set[str],
        latest_phrase: str,
        fps: float,
        latency_ms: float,
        speech_enabled: bool,
    ) -> None:
        height, width = frame.shape[:2]
        cv2.rectangle(frame, (0, 0), (width, 72), (24, 24, 24), -1)
        text_items: list[tuple[tuple[int, int], str, bool]] = []

        for key, detection in detections.items():
            item = class_for_prompt(detection.label)
            x1, y1, x2, y2 = detection.box
            confirmed = key in active_keys
            color = (32, 220, 90) if confirmed else (150, 150, 150)
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 3 if confirmed else 1)
            text_items.append(
                ((x1 + 4, max(78, y1 - 27)), f"{item.zh}  {detection.conf:.0%}", False)
            )

        sound = "语音开" if speech_enabled else "语音关"
        heading = f"NomaCook 实时识别  ·  {fps:.1f} FPS  ·  {latency_ms:.0f} ms  ·  {sound}"
        text_items.append(((14, 8), heading, True))
        if latest_phrase:
            text_items.append(((14, 40), f"✓ {latest_phrase}", False))
        else:
            text_items.append(((14, 40), "把菜品或厨房用品放到镜头前", False))

        if self._pil and self._font and self._small_font:
            Image, ImageDraw = self._pil
            image = Image.fromarray(frame[..., ::-1])
            draw = ImageDraw.Draw(image)
            for position, text, large in text_items:
                draw.text(
                    position,
                    text,
                    font=self._font if large else self._small_font,
                    fill=(255, 255, 255),
                    stroke_width=1,
                    stroke_fill=(0, 0, 0),
                )
            frame[:] = np.asarray(image)[..., ::-1]
        else:
            for position, text, large in text_items:
                ascii_text = text.encode("ascii", "replace").decode()
                cv2.putText(
                    frame,
                    ascii_text,
                    position,
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.65 if large else 0.52,
                    (255, 255, 255),
                    2,
                )

        cv2.putText(
            frame,
            "Q quit   M mute   R reset",
            (14, height - 14),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (230, 230, 230),
            1,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="实时识别菜品和厨房用品，并中文播报“某某已识别”"
    )
    parser.add_argument("--source", default="0", help="摄像头编号或视频路径")
    parser.add_argument(
        "--profile",
        choices=("demo", "dishes", "items", "full"),
        default="demo",
        help="demo=精选菜品+物品；dishes=菜品；items=物品；full=全部",
    )
    parser.add_argument("--device", default="auto", help="auto / mps / cuda / cpu")
    parser.add_argument("--conf", type=float, default=0.18, help="检测置信度阈值")
    parser.add_argument("--detect-every", type=int, default=3, help="每 N 帧检测一次")
    parser.add_argument("--window", type=int, default=3, help="时序确认窗口")
    parser.add_argument("--min-hits", type=int, default=2, help="窗口内最少命中次数")
    parser.add_argument("--release-misses", type=int, default=4, help="连续消失 N 次后释放")
    parser.add_argument("--cooldown", type=float, default=8.0, help="同物重新播报冷却秒数")
    parser.add_argument("--voice", default="Tingting", help="macOS say 中文音色")
    parser.add_argument(
        "--speech-backend",
        choices=("say", "iflytek"),
        default="say",
        help="实时播报后端",
    )
    parser.add_argument(
        "--language",
        default="zh-CN",
        help="讯飞播报目标语言，如 zh-CN、en-US、ja-JP",
    )
    parser.add_argument(
        "--iflytek-voice",
        default=None,
        help="讯飞控制台已授权的发音人 vcn；默认读取对应语言环境变量",
    )
    parser.add_argument(
        "--output-device",
        default=None,
        help="讯飞 PCM 播放设备编号或名称；默认使用系统输出设备",
    )
    parser.add_argument(
        "--dish-vlm",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="云端视觉识别成品菜（默认：有 GEMINI_API_KEY 时自动开启）",
    )
    parser.add_argument("--dish-interval", type=float, default=2.5, help="菜品识别间隔秒数")
    parser.add_argument("--dish-conf", type=float, default=0.72, help="菜品最低置信度")
    parser.add_argument("--no-speech", action="store_true", help="关闭语音，仅显示/打印")
    parser.add_argument("--no-display", action="store_true", help="不打开预览窗口")
    parser.add_argument("--max-frames", type=int, default=0, help="0=持续运行")
    parser.add_argument("--list-labels", action="store_true", help="列出当前可识别类别后退出")
    return parser


def run(args: argparse.Namespace) -> int:
    if args.detect_every < 1:
        raise ValueError("--detect-every 必须至少为 1")
    catalog = catalog_for_profile(args.profile)
    if args.list_labels:
        print("、".join(labels_zh(catalog)))
        return 0

    device = _device(args.device)
    wants_dishes = any(item.kind == "dish" for item in catalog)
    local_catalog = [item for item in catalog if item.kind != "dish"]
    if not local_catalog:  # dish-only mode still detects useful ROI anchors
        anchor_prompts = {"wok", "frying pan", "bowl", "plate"}
        local_catalog = [
            item for item in catalog_for_profile("items")
            if item.prompt in anchor_prompts
        ]
    vocab = [item.prompt for item in local_catalog]
    dish_candidates = labels_zh([item for item in catalog if item.kind == "dish"])
    dish_vlm = (
        gemini_is_configured() if args.dish_vlm is None else args.dish_vlm
    ) and wants_dishes
    source = _source(args.source)
    print(f"NomaCook 识别 demo：{device=}，{args.profile=}，类别 {len(labels_zh(catalog))} 个")
    print("可识别：" + "、".join(labels_zh(catalog)))
    if wants_dishes and not dish_vlm:
        print("提示：未开启 Gemini 菜品识别；本地厨房用品识别仍正常运行。")

    if args.speech_backend == "iflytek" and not args.no_speech:
        from server.iflytek_config import (
            iflytek_credentials,
            iflytek_tts_voice,
            normalize_language,
        )
        from server.voice.iflytek_translate import (
            IFlytekTranslator,
            translation_language_code,
        )
        from server.voice.iflytek_tts import IFlytekTTSProvider
        from server.voice.playback import play_stream_sync
        from server.voice.tts import SpeechRequest

        language = normalize_language(args.language)
        voice = iflytek_tts_voice(language, args.iflytek_voice)
        credentials = iflytek_credentials()
        assert credentials is not None
        provider = IFlytekTTSProvider(credentials)
        target = translation_language_code(language)
        translator = IFlytekTranslator() if target != "cn" else None
        output_device = _audio_device(args.output_device)

        def speak_with_iflytek(text: str) -> None:
            spoken = (
                translator.translate(text, source="cn", target=target)
                if translator is not None
                else text
            )
            play_stream_sync(
                provider,
                SpeechRequest(text=spoken, language=language, voice=voice),
                output_device=output_device,
            )

        speaker = SpeechAnnouncer(
            enabled=True, voice=voice, speaker=speak_with_iflytek
        )
        print(f"语音：讯飞流式 TTS，{language=}，{voice=}")
    else:
        speaker = SpeechAnnouncer(enabled=not args.no_speech, voice=args.voice)
    if not args.no_speech and not speaker.available:
        print("当前语音后端不可用，已自动降级为屏幕/终端提示。")

    detector = ObjectDetector(vocab=vocab, device=device, conf=args.conf)
    recognizer = StableRecognizer(
        window=args.window,
        min_hits=args.min_hits,
        min_confidence=args.conf,
        release_misses=args.release_misses,
        cooldown_seconds=args.cooldown,
    )
    dish_gate = DishConfirmationGate(min_confidence=args.dish_conf)

    cap = _open_capture(source)
    if not cap.isOpened():
        speaker.close()
        raise RuntimeError(f"无法打开视频源：{args.source}")

    overlay = _ChineseOverlay()
    detections: list[Detection] = []
    best_detections: dict[str, Detection] = {}
    latest_phrase = ""
    frame_index = 0
    fps = 0.0
    previous = time.perf_counter()
    last_dish_submit = float("-inf")
    dish_future: concurrent.futures.Future[LiveDishGuess] | None = None
    dish_pool = (
        concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="dish-vlm")
        if dish_vlm
        else None
    )

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break

            if frame_index % args.detect_every == 0:
                detections = detector.detect(frame)
                best_detections = _best_by_concept(detections)
                events = recognizer.update(
                    ((det.label, det.conf) for det in detections)
                )
                for event in events:
                    latest_phrase = event.phrase
                    print(f"✓ {event.phrase}  置信度 {event.confidence:.0%}")
                    speaker.speak(event.phrase)

            now = time.monotonic()
            if dish_future is not None and dish_future.done():
                try:
                    guess = dish_future.result()
                    event = dish_gate.update(
                        name=guess.name,
                        confidence=guess.confidence,
                        is_finished_dish=guess.is_finished_dish,
                        now=now,
                    )
                    state = (
                        f"菜品候选：{guess.name} {guess.confidence:.0%}"
                        if guess.is_finished_dish
                        else f"菜品门控：非成品（{guess.reason or '未见完整菜品'}）"
                    )
                    print(state)
                    if event is not None:
                        latest_phrase = event.phrase
                        print(f"✓ {event.phrase}  置信度 {event.confidence:.0%}")
                        speaker.speak(event.phrase)
                except Exception as exc:
                    print(f"菜品识别暂时失败（物品识别不受影响）：{exc}", file=sys.stderr)
                dish_future = None

            if (
                dish_pool is not None
                and dish_future is None
                and now - last_dish_submit >= args.dish_interval
            ):
                ok, jpeg = cv2.imencode(
                    ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 82]
                )
                if ok:
                    dish_future = dish_pool.submit(
                        identify_live_dish,
                        jpeg.tobytes(),
                        candidates=dish_candidates,
                    )
                    last_dish_submit = now

            current = time.perf_counter()
            instant_fps = 1.0 / max(current - previous, 1e-6)
            fps = instant_fps if fps == 0.0 else fps * 0.9 + instant_fps * 0.1
            previous = current

            if not args.no_display:
                overlay.draw(
                    frame,
                    detections=best_detections,
                    active_keys=recognizer.active_keys,
                    latest_phrase=latest_phrase,
                    fps=fps,
                    latency_ms=detector.last_latency_ms,
                    speech_enabled=speaker.enabled,
                )
                preview = frame
                if frame.shape[1] > 1280:
                    scale = 1280 / frame.shape[1]
                    preview = cv2.resize(frame, None, fx=scale, fy=scale)
                cv2.imshow(WINDOW_TITLE, preview)
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    break
                if key == ord("m"):
                    speaker.set_enabled(not speaker.enabled)
                    print("语音已开启" if speaker.enabled else "语音已关闭")
                if key == ord("r"):
                    recognizer.reset()
                    dish_gate.reset()
                    latest_phrase = ""
                    print("识别状态已重置")

            frame_index += 1
            if args.max_frames and frame_index >= args.max_frames:
                break
    finally:
        cap.release()
        speaker.close()
        if dish_pool is not None:
            dish_pool.shutdown(wait=True, cancel_futures=True)
        if not args.no_display:
            cv2.destroyAllWindows()

    if speaker.last_error:
        print(f"语音播报出现错误（识别未受影响）：{speaker.last_error}", file=sys.stderr)
    print(f"demo 已结束，共处理 {frame_index} 帧。")
    return 0


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        raise SystemExit(run(args))
    except (RuntimeError, ValueError) as exc:
        parser.exit(2, f"错误：{exc}\n")


if __name__ == "__main__":
    main()
