from __future__ import annotations

import threading

import pytest

from perception.realtime_recognition import (
    DishConfirmationGate,
    SpeechAnnouncer,
    StableRecognizer,
    catalog_for_profile,
    class_for_prompt,
)


def test_two_hits_are_required_before_recognition() -> None:
    recognizer = StableRecognizer(window=3, min_hits=2, min_confidence=0.2)

    assert recognizer.update([("bowl", 0.70)], now=0.0) == []
    events = recognizer.update([("bowl", 0.60)], now=0.1)

    assert [event.zh for event in events] == ["碗"]
    assert events[0].phrase == "碗已识别"
    assert events[0].confidence == pytest.approx(0.65)


def test_continuous_detection_is_announced_only_once() -> None:
    recognizer = StableRecognizer(window=2, min_hits=1, cooldown_seconds=0.0)

    first = recognizer.update([("spatula", 0.8)], now=0.0)
    second = recognizer.update([("spatula", 0.9)], now=10.0)

    assert len(first) == 1
    assert second == []


def test_disappearance_and_cooldown_allow_a_later_reannouncement() -> None:
    recognizer = StableRecognizer(
        window=2,
        min_hits=1,
        release_misses=2,
        cooldown_seconds=5.0,
    )
    assert len(recognizer.update([("egg", 0.8)], now=0.0)) == 1
    recognizer.update([], now=1.0)
    recognizer.update([], now=2.0)

    assert recognizer.update([("egg", 0.8)], now=3.0) == []
    recognizer.update([], now=4.0)
    recognizer.update([], now=5.0)
    assert len(recognizer.update([("egg", 0.8)], now=6.0)) == 1


def test_synonymous_pan_prompts_share_one_canonical_concept() -> None:
    recognizer = StableRecognizer(window=1, min_hits=1)

    events = recognizer.update(
        [("wok", 0.55), ("frying pan", 0.72)], now=0.0
    )

    assert len(events) == 1
    assert events[0].key == "wok"
    assert events[0].confidence == 0.72


def test_dish_phrase_and_demo_profile_are_user_facing() -> None:
    dish = class_for_prompt("tomato and scrambled eggs")
    labels = {item.zh for item in catalog_for_profile("demo")}

    assert dish.kind == "dish"
    assert "番茄炒鸡蛋" in labels
    assert "炒锅" in labels


def test_dish_gate_rejects_work_in_progress_and_confirms_repeated_name() -> None:
    gate = DishConfirmationGate(
        min_confidence=0.7,
        instant_confidence=0.9,
        min_hits=2,
    )

    assert gate.update(
        name="番茄炒鸡蛋", confidence=0.95, is_finished_dish=False, now=0.0
    ) is None
    assert gate.update(
        name="番茄炒鸡蛋", confidence=0.78, is_finished_dish=True, now=1.0
    ) is None
    event = gate.update(
        name="番茄炒鸡蛋", confidence=0.82, is_finished_dish=True, now=2.0
    )

    assert event is not None
    assert event.phrase == "番茄炒鸡蛋，菜品已识别"


def test_dish_gate_accepts_one_very_high_confidence_result() -> None:
    gate = DishConfirmationGate(instant_confidence=0.88)

    event = gate.update(
        name="蛋炒饭", confidence=0.91, is_finished_dish=True, now=0.0
    )

    assert event is not None
    assert event.zh == "蛋炒饭"


def test_speech_announcer_keeps_only_latest_pending_message() -> None:
    heard: list[str] = []
    first_started = threading.Event()
    release_first = threading.Event()
    latest_finished = threading.Event()

    def speaker(text: str) -> None:
        heard.append(text)
        if text == "first":
            first_started.set()
            assert release_first.wait(1.0)
        if text == "latest":
            latest_finished.set()

    announcer = SpeechAnnouncer(speaker=speaker)
    try:
        assert announcer.speak("first")
        assert first_started.wait(1.0)
        assert announcer.speak("stale")
        assert announcer.speak("latest")

        release_first.set()
        assert latest_finished.wait(1.0)
        assert heard == ["first", "latest"]
    finally:
        release_first.set()
        announcer.close()


def test_speech_announcer_close_drops_pending_and_rejects_new_messages() -> None:
    heard: list[str] = []
    first_started = threading.Event()
    release_first = threading.Event()
    first_finished = threading.Event()

    def speaker(text: str) -> None:
        heard.append(text)
        first_started.set()
        assert release_first.wait(1.0)
        first_finished.set()

    announcer = SpeechAnnouncer(speaker=speaker)
    assert announcer.speak("first")
    assert first_started.wait(1.0)
    assert announcer.speak("pending")

    announcer.close(timeout=0.01)
    assert not announcer.speak("after close")
    release_first.set()
    assert first_finished.wait(1.0)
    announcer.close()

    assert heard == ["first"]
