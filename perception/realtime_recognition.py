"""Stable, demo-friendly recognition events and non-blocking speech.

The detector intentionally stays in ``perception.detector``.  This module is
the small amount of product logic needed between noisy per-frame detections
and a human-facing "...已识别" announcement:

1. map English YOLO-World prompts to canonical Chinese concepts;
2. require repeated hits across a short temporal window;
3. suppress duplicate announcements while an object remains visible; and
4. run the selected speech backend on a worker thread so inference never waits.
"""

from __future__ import annotations

import queue
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Iterable, Literal, Sequence


RecognitionKind = Literal["dish", "kitchenware", "ingredient"]


@dataclass(frozen=True)
class RecognitionClass:
    key: str
    prompt: str
    zh: str
    kind: RecognitionKind


# YOLO-World uses English CLIP prompts.  More than one prompt may map to the
# same concept; canonicalization prevents "wok" and "frying pan" from being
# announced twice for the same pan.
CATALOG: tuple[RecognitionClass, ...] = (
    RecognitionClass("tomato_egg", "tomato and scrambled eggs", "番茄炒鸡蛋", "dish"),
    RecognitionClass("egg_fried_rice", "egg fried rice", "蛋炒饭", "dish"),
    RecognitionClass("yangzhou_fried_rice", "Yangzhou fried rice", "扬州炒饭", "dish"),
    RecognitionClass("shredded_potato", "hot and sour shredded potatoes", "酸辣土豆丝", "dish"),
    RecognitionClass("stir_fried_cabbage", "stir-fried cabbage", "手撕包菜", "dish"),
    RecognitionClass("lotus_root", "stir-fried diced lotus root", "小炒藕丁", "dish"),
    RecognitionClass("spinach_egg", "spinach with scrambled eggs", "菠菜炒鸡蛋", "dish"),
    RecognitionClass("fried_noodles", "stir-fried instant noodles", "炒方便面", "dish"),
    RecognitionClass("omurice", "omurice", "蛋包饭", "dish"),
    RecognitionClass("cola_fried_rice", "cola fried rice", "可乐炒饭", "dish"),
    RecognitionClass("wok", "wok", "炒锅", "kitchenware"),
    RecognitionClass("wok", "frying pan", "炒锅", "kitchenware"),
    RecognitionClass("pot", "pot", "锅", "kitchenware"),
    RecognitionClass("bowl", "bowl", "碗", "kitchenware"),
    RecognitionClass("plate", "plate", "盘子", "kitchenware"),
    RecognitionClass("cup", "cup", "杯子", "kitchenware"),
    RecognitionClass("cutting_board", "cutting board", "砧板", "kitchenware"),
    RecognitionClass("knife", "kitchen knife", "菜刀", "kitchenware"),
    RecognitionClass("chopsticks", "chopsticks", "筷子", "kitchenware"),
    RecognitionClass("spatula", "spatula", "锅铲", "kitchenware"),
    RecognitionClass("ladle", "ladle", "汤勺", "kitchenware"),
    RecognitionClass("soy_sauce", "soy sauce bottle", "酱油瓶", "kitchenware"),
    RecognitionClass("oil_bottle", "oil bottle", "油瓶", "kitchenware"),
    RecognitionClass("salt_shaker", "salt shaker", "盐罐", "kitchenware"),
    RecognitionClass("rice_cooker", "rice cooker", "电饭煲", "kitchenware"),
    RecognitionClass("tomato", "tomato", "番茄", "ingredient"),
    RecognitionClass("egg", "egg", "鸡蛋", "ingredient"),
    RecognitionClass("rice", "cooked rice", "米饭", "ingredient"),
    RecognitionClass("scallion", "scallion", "葱", "ingredient"),
    RecognitionClass("garlic", "garlic", "蒜", "ingredient"),
)


_DEMO_KEYS = {
    "tomato_egg",
    "egg_fried_rice",
    "wok",
    "bowl",
    "plate",
    "cutting_board",
    "knife",
    "chopsticks",
    "spatula",
    "soy_sauce",
    "oil_bottle",
    "tomato",
    "egg",
}


def catalog_for_profile(profile: str) -> list[RecognitionClass]:
    """Return the prompt catalog for a CLI profile, preserving order."""

    if profile == "demo":
        return [item for item in CATALOG if item.key in _DEMO_KEYS]
    if profile == "dishes":
        return [item for item in CATALOG if item.kind == "dish"]
    if profile == "items":
        return [item for item in CATALOG if item.kind != "dish"]
    if profile == "full":
        return list(CATALOG)
    raise ValueError(f"unknown recognition profile: {profile!r}")


_BY_PROMPT = {item.prompt: item for item in CATALOG}
def class_for_prompt(prompt: str) -> RecognitionClass:
    """Map a detector label to a concept; preserve custom CLI labels."""

    return _BY_PROMPT.get(
        prompt,
        RecognitionClass(prompt, prompt, prompt, "kitchenware"),
    )


@dataclass(frozen=True)
class Recognition:
    key: str
    zh: str
    kind: RecognitionKind
    confidence: float

    @property
    def phrase(self) -> str:
        if self.kind == "dish":
            return f"{self.zh}，菜品已识别"
        return f"{self.zh}已识别"


@dataclass
class _State:
    history: deque[float]
    misses: int = 0
    active: bool = False
    last_announced_at: float | None = None


class StableRecognizer:
    """Convert jittery frame detections into edge-triggered recognitions."""

    def __init__(
        self,
        *,
        window: int = 3,
        min_hits: int = 2,
        min_confidence: float = 0.18,
        release_misses: int = 4,
        cooldown_seconds: float = 8.0,
    ) -> None:
        if window < 1:
            raise ValueError("window must be at least 1")
        if min_hits < 1 or min_hits > window:
            raise ValueError("min_hits must be between 1 and window")
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError("min_confidence must be between 0 and 1")
        if release_misses < 1:
            raise ValueError("release_misses must be at least 1")
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds cannot be negative")
        self.window = window
        self.min_hits = min_hits
        self.min_confidence = min_confidence
        self.release_misses = release_misses
        self.cooldown_seconds = cooldown_seconds
        self._states: dict[str, _State] = {}

    def reset(self) -> None:
        self._states.clear()

    @property
    def active_keys(self) -> set[str]:
        return {key for key, state in self._states.items() if state.active}

    def update(
        self,
        detections: Iterable[tuple[str, float]],
        *,
        now: float | None = None,
    ) -> list[Recognition]:
        """Process one detector tick and return only newly confirmed concepts."""

        timestamp = time.monotonic() if now is None else now
        best: dict[str, tuple[RecognitionClass, float]] = {}
        for prompt, confidence in detections:
            item = class_for_prompt(prompt)
            score = float(confidence)
            if score < self.min_confidence:
                continue
            previous = best.get(item.key)
            if previous is None or score > previous[1]:
                best[item.key] = (item, score)

        emitted: list[Recognition] = []
        for key in set(self._states) | set(best):
            state = self._states.setdefault(
                key, _State(history=deque(maxlen=self.window))
            )
            current = best.get(key)
            if current is None:
                state.history.append(0.0)
                state.misses += 1
                if state.misses >= self.release_misses:
                    state.active = False
                continue

            item, confidence = current
            state.history.append(confidence)
            state.misses = 0
            hits = [score for score in state.history if score >= self.min_confidence]
            if state.active or len(hits) < self.min_hits:
                continue

            state.active = True
            cooled_down = (
                state.last_announced_at is None
                or timestamp - state.last_announced_at >= self.cooldown_seconds
            )
            if cooled_down:
                state.last_announced_at = timestamp
                emitted.append(
                    Recognition(
                        key=key,
                        zh=item.zh,
                        kind=item.kind,
                        confidence=sum(hits) / len(hits),
                    )
                )

        return sorted(emitted, key=lambda event: event.confidence, reverse=True)


class DishConfirmationGate:
    """Require a strong or repeated VLM answer before announcing a dish."""

    def __init__(
        self,
        *,
        min_confidence: float = 0.72,
        instant_confidence: float = 0.88,
        min_hits: int = 2,
        cooldown_seconds: float = 30.0,
    ) -> None:
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError("min_confidence must be between 0 and 1")
        if not min_confidence <= instant_confidence <= 1.0:
            raise ValueError("instant_confidence must be >= min_confidence")
        if min_hits < 1:
            raise ValueError("min_hits must be at least 1")
        self.min_confidence = min_confidence
        self.instant_confidence = instant_confidence
        self.min_hits = min_hits
        self.cooldown_seconds = cooldown_seconds
        self._candidate = ""
        self._scores: list[float] = []
        self._active = ""
        self._last_announced: dict[str, float] = {}

    def reset(self) -> None:
        self._candidate = ""
        self._scores = []
        self._active = ""

    def update(
        self,
        *,
        name: str,
        confidence: float,
        is_finished_dish: bool,
        now: float | None = None,
    ) -> Recognition | None:
        timestamp = time.monotonic() if now is None else now
        cleaned = name.strip()
        score = float(confidence)
        if not is_finished_dish or not cleaned or score < self.min_confidence:
            self.reset()
            return None

        if cleaned != self._candidate:
            self._candidate = cleaned
            self._scores = [score]
        else:
            self._scores.append(score)

        if self._active == cleaned:
            return None
        enough_evidence = (
            score >= self.instant_confidence or len(self._scores) >= self.min_hits
        )
        if not enough_evidence:
            return None

        last = self._last_announced.get(cleaned)
        if last is not None and timestamp - last < self.cooldown_seconds:
            self._active = cleaned
            return None

        self._active = cleaned
        self._last_announced[cleaned] = timestamp
        return Recognition(
            key=f"dish:{cleaned}",
            zh=cleaned,
            kind="dish",
            confidence=sum(self._scores) / len(self._scores),
        )


class SpeechAnnouncer:
    """Speak the latest pending message without blocking video inference."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        voice: str = "Tingting",
        command: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        speaker: Callable[[str], None] | None = None,
    ) -> None:
        resolved = None if speaker is not None else command or shutil.which("say")
        self.voice = voice
        self.available = speaker is not None or resolved is not None
        self.enabled = enabled and self.available
        self._command = resolved
        self._runner = runner
        self._speaker = speaker
        # Speech (especially translation + cloud TTS + playback) can take longer
        # than detections arrive.  Keep only one pending utterance so stale
        # recognition chatter cannot accumulate behind the message being spoken.
        self._queue: queue.Queue[str | None] = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._closed = False
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None
        if self.enabled:
            self._start()

    def _start(self) -> None:
        with self._lock:
            if self._closed or self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._worker, name="nomacook-speech", daemon=True
            )
            self._thread.start()

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            if self._closed:
                self.enabled = False
                return
            self.enabled = enabled and self.available
            should_start = self.enabled and self._thread is None
        if should_start:
            self._start()

    def speak(self, text: str) -> bool:
        cleaned = text.strip()
        if not cleaned:
            return False
        with self._lock:
            if self._closed or not self.enabled:
                return False
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            self._queue.put_nowait(cleaned)
            return True

    def _worker(self) -> None:
        while True:
            text = self._queue.get()
            if text is None:
                return
            if not self.enabled:
                continue
            try:
                if self._speaker is not None:
                    self._speaker(text)
                    continue
                result = self._runner(
                    [str(self._command), "-v", self.voice, text],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if result.returncode != 0:
                    self.last_error = result.stderr.strip() or "say failed"
            except Exception as exc:  # speech must never crash perception
                self.last_error = str(exc)

    def close(self, timeout: float = 3.0) -> None:
        with self._lock:
            self.enabled = False
            if not self._closed:
                self._closed = True
                # Drop pending chatter; finish only an utterance whose callback
                # has already begun, then let the worker consume the sentinel.
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass
                if self._thread is not None:
                    self._queue.put_nowait(None)
            thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)


def prompts_for_profile(profile: str) -> list[str]:
    return [item.prompt for item in catalog_for_profile(profile)]


def labels_zh(items: Sequence[RecognitionClass]) -> list[str]:
    """Return unique Chinese labels in stable catalog order."""

    return list(dict.fromkeys(item.zh for item in items))
