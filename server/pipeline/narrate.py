"""Turn one run's narration.json into a mixed Chinese voice track and mux it."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


def intro_item(recipe) -> dict:
    first = recipe.steps[0]
    return {"pts_ms": 0.0, "kind": "intro",
            "text": f"开始制作{recipe.dish}。第一步，{first.instruction}"}


def transition_item(recipe, completed_step_id: str, next_step_id: str,
                    pts_ms: float) -> dict:
    steps = {step.id: step for step in recipe.steps}
    return {"pts_ms": pts_ms, "kind": "step",
            "text": f"这一步完成了。下一步，{steps[next_step_id].instruction}"}


def question_item(question: str, pts_ms: float) -> dict:
    return {"pts_ms": pts_ms, "kind": "question", "text": question}


def complete_item(pts_ms: float) -> dict:
    return {"pts_ms": pts_ms, "kind": "complete",
            "text": "全部步骤完成，可以盛盘上桌了。妈，我会做饭了。"}


def schedule(items: list[dict], durations_ms: list[float],
             gap_ms: float = 300.0) -> list[float]:
    starts: list[float] = []
    cursor = 0.0
    for item, duration in zip(items, durations_ms):
        start = max(float(item["pts_ms"]), cursor)
        starts.append(start)
        cursor = start + duration + gap_ms
    return starts


def synthesize_say(text: str, out_path: Path, voice: str = "Tingting") -> None:
    result = subprocess.run(
        ["say", "-v", voice, "-o", str(out_path), text],
        capture_output=True, text=True)
    if result.returncode != 0 or not out_path.exists():
        raise RuntimeError(
            f"say failed for voice {voice!r}: {result.stderr.strip()}; "
            "list voices with: say -v '?'")


def synthesize_gemini(text: str, out_path: Path) -> None:
    import wave

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    model = os.getenv("GEMINI_TTS_MODEL", "gemini-3.1-flash-tts-preview")
    response = client.models.generate_content(
        model=model, contents=text,
        config=types.GenerateContentConfig(response_modalities=["AUDIO"]))
    part = response.candidates[0].content.parts[0]
    pcm = part.inline_data.data
    with wave.open(str(out_path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24_000)
        stream.writeframes(pcm)
    client.close()


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


def narrate_run(run_root: Path, backend: str, voice: str = "Tingting") -> Path:
    _require_ffmpeg()
    items = json.loads((run_root / "narration.json").read_text(encoding="utf-8"))
    if not items:
        raise RuntimeError("narration.json is empty; nothing to narrate")
    clips_dir = run_root / "narration_clips"
    clips_dir.mkdir(exist_ok=True)

    clips: list[Path] = []
    for i, item in enumerate(items):
        if backend == "say":
            clip = clips_dir / f"clip_{i:03d}.aiff"
            synthesize_say(item["text"], clip, voice=voice)
        elif backend == "gemini":
            clip = clips_dir / f"clip_{i:03d}.wav"
            synthesize_gemini(item["text"], clip)
        else:
            raise ValueError(f"unknown narrate backend {backend!r}")
        clips.append(clip)

    starts = schedule(items, [_duration_ms(c) for c in clips])
    inputs: list[str] = ["-i", str(run_root / "annotated.mp4")]
    filters: list[str] = []
    labels: list[str] = []
    for i, (clip, start) in enumerate(zip(clips, starts)):
        inputs += ["-i", str(clip)]
        delay = max(0, int(round(start)))
        filters.append(f"[{i + 1}:a]adelay={delay}|{delay}[a{i}]")
        labels.append(f"[a{i}]")
    filters.append(
        "".join(labels) + f"amix=inputs={len(clips)}:normalize=0[mix]")
    out_path = run_root / "annotated_narrated.mp4"
    subprocess.run(
        ["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(filters),
         "-map", "0:v", "-map", "[mix]", "-c:v", "copy", "-c:a", "aac",
         str(out_path)],
        capture_output=True, text=True, check=True)
    return out_path
