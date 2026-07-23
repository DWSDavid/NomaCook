from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from server.engine.models import SessionContext
from server.engine import load_recipe
from server.perception import (
    ContextualVocabularyController,
    build_detection_context,
    canonicalize_detections,
)


@dataclass
class FakeDetection:
    label: str
    conf: float
    box: tuple[int, int, int, int]


class FakeDetector:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def set_vocab(self, vocab: list[str]) -> None:
        self.calls.append(vocab)


def seasoning_context():
    recipe = load_recipe("sop/fried_rice.json")
    step = next(step for step in recipe.steps if step.id == "step_04_season")
    context = SessionContext(
        session_id="ses_test",
        recipe_version_id=recipe.recipe_version_id,
        current_step_id=step.id,
        started_at=datetime(2026, 7, 23, tzinfo=UTC),
        active_objects=step.objects_involved,
        context_version=9,
    )
    return recipe, context


def test_seasoning_step_uses_concrete_prompts_without_unrelated_cookware() -> None:
    recipe, context = seasoning_context()
    detection_context = build_detection_context(context, recipe)

    assert detection_context.step_id == "step_04_season"
    assert len(detection_context.prompts) <= 18
    assert "soy sauce bottle" in detection_context.prompts
    assert "salt shaker" in detection_context.prompts
    assert "pepper container" in detection_context.prompts
    assert "human hand" in detection_context.prompts
    assert "cooking oil bottle" in detection_context.prompts
    assert "vinegar bottle" in detection_context.prompts
    assert "pot lid" not in detection_context.prompts
    assert "cooking pot" not in detection_context.prompts


def test_alias_predictions_are_canonicalized_and_overlap_is_merged() -> None:
    recipe, context = seasoning_context()
    detection_context = build_detection_context(context, recipe)
    raw = [
        FakeDetection("soy sauce bottle", 0.72, (10, 10, 100, 200)),
        FakeDetection("dark condiment bottle", 0.66, (12, 12, 101, 199)),
        FakeDetection("cooking oil bottle", 0.61, (220, 10, 300, 200)),
        FakeDetection("salt shaker", 0.10, (320, 20, 360, 100)),
    ]

    normalized = canonicalize_detections(raw, detection_context)
    assert [item.canonical_label for item in normalized] == [
        "soy_sauce_bottle",
        "oil_bottle",
    ]
    assert normalized[0].role == "primary"
    assert normalized[1].role == "confuser"


def test_controller_updates_detector_only_when_prompt_set_changes() -> None:
    recipe, context = seasoning_context()
    detection_context = build_detection_context(context, recipe)
    detector = FakeDetector()
    controller = ContextualVocabularyController(detector)

    assert controller.sync(detection_context)
    assert not controller.sync(detection_context.model_copy(update={"context_version": 10}))
    assert len(detector.calls) == 1

    prepare_step = recipe.steps[0]
    prepare_context = context.model_copy(
        update={
            "current_step_id": prepare_step.id,
            "active_objects": prepare_step.objects_involved,
            "context_version": 11,
        }
    )
    assert controller.sync(build_detection_context(prepare_context, recipe))
    assert len(detector.calls) == 2


def test_prompt_budget_keeps_at_least_one_prompt_for_every_primary_object() -> None:
    recipe, context = seasoning_context()
    detection_context = build_detection_context(
        context, recipe, include_bottle_confusers=True, max_prompts=6
    )
    primary = [target for target in detection_context.targets if target.role == "primary"]
    assert len(primary) == len(context.active_objects)
    assert all(target.prompts for target in primary)
    assert len(detection_context.prompts) == 6
