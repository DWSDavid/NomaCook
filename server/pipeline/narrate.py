"""Turn one run's narration.json into a mixed Chinese voice track and mux it."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

from server.gemini_config import gemini_api_key, gemini_setting


_ZH_STEP_NUMBERS = ("零", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十")


def _step_label(step) -> str:
    number = (
        _ZH_STEP_NUMBERS[step.sequence]
        if 0 < step.sequence < len(_ZH_STEP_NUMBERS)
        else str(step.sequence)
    )
    return f"第{number}步，{step.title}。" if step.title else f"第{number}步。"


def intro_item(recipe) -> dict:
    first = recipe.steps[0]
    label = _step_label(first)
    return {"pts_ms": 0.0, "kind": "intro",
            "text": f"好，咱们开始做{recipe.dish}。{label}{first.instruction}"}


def transition_item(recipe, completed_step_id: str, next_step_id: str,
                    pts_ms: float, *, include_instruction: bool = True) -> dict:
    steps = {step.id: step for step in recipe.steps}
    completed = steps[completed_step_id]
    nxt = steps[next_step_id]
    confirmation = completed.completion_message or "嗯，这步搞定了。"
    label = _step_label(nxt)
    text = (
        f"{confirmation}{label}{nxt.instruction}"
        if include_instruction
        else confirmation
    )
    return {"pts_ms": pts_ms, "kind": "step", "text": text}


def preview_item(recipe, current_step_id: str, pts_ms: float) -> dict | None:
    """Heads-up while the current step is ALMOST done: pre-announce the next
    step so the user can get ahead, instead of only hearing it after the
    engine confirms completion. Returns None on the last step."""
    steps = list(recipe.steps)
    index = next(
        (i for i, step in enumerate(steps) if step.id == current_step_id), None
    )
    if index is None or index + 1 >= len(steps):
        return None
    nxt = steps[index + 1]
    label = _step_label(nxt)
    return {"pts_ms": pts_ms, "kind": "preview",
            "text": f"这一步快好了。下一步是{label}等我确认后再开始。"}


def remark_item(text: str, pts_ms: float) -> dict:
    """A proactive one-liner sourced from the Gemini VLM (risk warning or
    coach_comment) — the channel that makes Gemini audible to the user."""
    return {"pts_ms": pts_ms, "kind": "remark", "text": text}


def question_item(question: str, pts_ms: float) -> dict:
    return {"pts_ms": pts_ms, "kind": "question", "text": question}


def complete_item(pts_ms: float, recipe=None) -> dict:
    text = "全部步骤完成，可以盛盘上桌了。"
    if recipe is not None:
        text = f"{recipe.dish}做好了。妈，我会做饭了。"
    return {"pts_ms": pts_ms, "kind": "complete",
            "text": text}


def schedule(items: list[dict], durations_ms: list[float],
             gap_ms: float = 300.0) -> list[float]:
    starts: list[float] = []
    cursor = 0.0
    for item, duration in zip(items, durations_ms):
        start = max(float(item["pts_ms"]), cursor)
        starts.append(start)
        cursor = start + duration + gap_ms
    return starts


def fit_schedule(
    items: list[dict],
    durations_ms: list[float],
    *,
    end_ms: float,
    gap_ms: float = 300.0,
    max_optional_lateness_ms: float = 3_000.0,
    mandatory_guard_ms: float = 2_000.0,
) -> tuple[list[int], list[float]]:
    """Prioritize step-changing speech and omit optional cues that would
    push a later instruction away from the matching picture."""
    if len(items) != len(durations_ms):
        raise ValueError("items and durations_ms must have the same length")
    mandatory = {"intro", "step", "complete"}
    selected: list[int] = []
    starts: list[float] = []
    cursor = 0.0

    for index, (item, duration) in enumerate(zip(items, durations_ms)):
        intended = float(item["pts_ms"])
        start = max(intended, cursor)
        if item.get("kind") not in mandatory:
            next_mandatory = next(
                (
                    float(later["pts_ms"])
                    for later in items[index + 1:]
                    if later.get("kind") in mandatory
                ),
                end_ms,
            )
            latest_end = min(next_mandatory, end_ms)
            too_late = start - intended > max_optional_lateness_ms
            collides = (
                start + duration + gap_ms + mandatory_guard_ms > latest_end
            )
            if too_late or collides:
                continue
        selected.append(index)
        starts.append(start)
        cursor = start + duration + gap_ms
    return selected, starts


def synthesize_say(text: str, out_path: Path, voice: str = "Tingting") -> None:
    result = subprocess.run(
        ["say", "-v", voice, "-o", str(out_path), text],
        capture_output=True, text=True)
    if result.returncode != 0 or not out_path.exists():
        raise RuntimeError(
            f"say failed for voice {voice!r}: {result.stderr.strip()}; "
            "list voices with: say -v '?'")


# Style directive prepended to every TTS call. TTS models take the speaking
# style from the prompt text itself; without it the read is flat "AI 播音腔".
TTS_STYLE_PREFIX = (
    "只朗读最后的【台词】，不要读出这些要求。说话像熟悉的家人在厨房旁边搭把手："
    "温暖、松弛、自然，有真实聊天的轻重和停顿；不要客服腔、播音腔或逐字念稿，"
    "不要夸张卖萌。提醒下一步时轻轻带过，不催用户，语速中等偏慢。\n【台词】"
)


def synthesize_gemini(text: str, out_path: Path, *, attempts: int = 4) -> None:
    import wave

    from google import genai
    from google.genai import types

    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    model = gemini_setting("GEMINI_TTS_MODEL", "gemini-3.1-flash-tts-preview")
    voice = gemini_setting("GEMINI_TTS_VOICE", "Aoede")
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        client = genai.Client(api_key=gemini_api_key())
        try:
            response = client.models.generate_content(
                model=model, contents=f"{TTS_STYLE_PREFIX}{text}",
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                voice_name=voice))),
                ))
            part = response.candidates[0].content.parts[0]
            pcm = part.inline_data.data
            with wave.open(str(out_path), "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(24_000)
                stream.writeframes(pcm)
            return
        except Exception as exc:  # noqa: BLE001 - transient SDK/network errors
            last_error = exc
            if attempt == attempts:
                break
            delay = 2 ** attempt
            print(
                f"Gemini TTS attempt {attempt}/{attempts} failed; "
                f"retrying in {delay}s: {exc}"
            )
            time.sleep(delay)
        finally:
            client.close()
    assert last_error is not None
    raise last_error


def _require_ffmpeg() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise RuntimeError(
                f"{tool} not found; install it first: brew install ffmpeg")


def _duration_ms(clip: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(clip)],
        capture_output=True, text=True, check=True)
    return float(out.stdout.strip()) * 1000.0


def _read_clip_meta(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _playable_clip(path: Path, *, minimum_bytes: int) -> bool:
    if not path.exists() or path.stat().st_size <= minimum_bytes:
        return False
    try:
        return _duration_ms(path) > 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def narrate_run(
    run_root: Path,
    backend: str,
    voice: str = "Tingting",
    *,
    language: str = "zh-CN",
    iflytek_voice: str | None = None,
) -> Path:
    _require_ffmpeg()
    items = json.loads((run_root / "narration.json").read_text(encoding="utf-8"))
    if not items:
        raise RuntimeError("narration.json is empty; nothing to narrate")
    clips_dir = run_root / "narration_clips"
    clips_dir.mkdir(exist_ok=True)

    if backend == "say":
        backend_voice = voice
        clip_suffix = ".aiff"
        output_language = "zh-CN"
        style_version = "say-v1"
    elif backend == "gemini":
        backend_voice = str(gemini_setting("GEMINI_TTS_VOICE", "Aoede"))
        clip_suffix = ".wav"
        output_language = "zh-CN"
        style_version = TTS_STYLE_PREFIX
    elif backend == "iflytek":
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

        output_language = normalize_language(language)
        backend_voice = iflytek_tts_voice(output_language, iflytek_voice)
        credentials = iflytek_credentials()
        assert credentials is not None
        iflytek_provider = IFlytekTTSProvider(credentials)
        translation_target = translation_language_code(output_language)
        iflytek_translator = (
            IFlytekTranslator() if translation_target != "cn" else None
        )
        clip_suffix = ".wav"
        style_version = "iflytek-online-tts-pcm16k-v1"
    else:
        raise ValueError(f"unknown narrate backend {backend!r}")

    clips: list[Path] = []
    spoken_texts: list[str] = []
    for i, item in enumerate(items):
        transcript = clips_dir / f"clip_{i:03d}.txt"
        meta_path = clips_dir / f"clip_{i:03d}.meta.json"
        clip = clips_dir / f"clip_{i:03d}{clip_suffix}"
        cache_spec = {
            "source_text": item["text"],
            "backend": backend,
            "language": output_language,
            "voice": backend_voice,
            "style_version": style_version,
        }
        cached = _read_clip_meta(meta_path)
        cache_matches = bool(
            cached
            and all(cached.get(key) == value for key, value in cache_spec.items())
            and isinstance(cached.get("spoken_text"), str)
        )
        spoken_text = str(cached["spoken_text"]) if cache_matches else item["text"]
        if backend == "iflytek" and not cache_matches and iflytek_translator:
            spoken_text = iflytek_translator.translate(
                item["text"], source="cn", target=translation_target
            )

        if backend == "say":
            playable = _playable_clip(clip, minimum_bytes=0)
            if not (cache_matches and playable):
                synthesize_say(spoken_text, clip, voice=voice)
        elif backend == "gemini":
            playable = _playable_clip(clip, minimum_bytes=44)
            if not (cache_matches and playable):
                synthesize_gemini(spoken_text, clip)
        elif backend == "iflytek":
            from server.voice.tts import SpeechRequest, write_wav_sync

            playable = _playable_clip(clip, minimum_bytes=44)
            if not (cache_matches and playable):
                write_wav_sync(
                    iflytek_provider,
                    SpeechRequest(
                        text=spoken_text,
                        language=output_language,
                        voice=backend_voice,
                    ),
                    clip,
                )
        transcript.write_text(spoken_text, encoding="utf-8")
        meta_path.write_text(
            json.dumps(
                {**cache_spec, "spoken_text": spoken_text},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        clips.append(clip)
        spoken_texts.append(spoken_text)

    durations = [_duration_ms(c) for c in clips]
    video_duration = _duration_ms(run_root / "annotated.mp4")
    selected, starts = fit_schedule(items, durations, end_ms=video_duration)
    selected_starts = dict(zip(selected, starts))
    schedule_rows = [
        {
            **item,
            "spoken_text": spoken_texts[i],
            "language": output_language,
            "voice": backend_voice,
            "duration_ms": round(durations[i], 1),
            "selected": i in selected_starts,
            "actual_start_ms": (
                round(selected_starts[i], 1) if i in selected_starts else None
            ),
        }
        for i, item in enumerate(items)
    ]
    (run_root / "narration_schedule.json").write_text(
        json.dumps(schedule_rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    inputs: list[str] = ["-i", str(run_root / "annotated.mp4")]
    filters: list[str] = []
    labels: list[str] = []
    for mix_index, (item_index, start) in enumerate(zip(selected, starts)):
        clip = clips[item_index]
        inputs += ["-i", str(clip)]
        delay = max(0, int(round(start)))
        filters.append(
            f"[{mix_index + 1}:a]adelay={delay}|{delay}[a{mix_index}]"
        )
        labels.append(f"[a{mix_index}]")
    filters.append(
        "".join(labels)
        + f"amix=inputs={len(selected)}:normalize=0:duration=longest,apad[mix]"
    )
    out_path = run_root / "annotated_narrated.mp4"
    subprocess.run(
        ["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(filters),
         "-map", "0:v", "-map", "[mix]", "-c:v", "copy", "-c:a", "aac",
         "-shortest", str(out_path)],
        capture_output=True, text=True, check=True)
    return out_path
