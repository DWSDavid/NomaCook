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
from server.vlm.detection_context import (
    confident_detection_items,
    curate_detections,
    format_scene_context,
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


@dataclass
class SceneDetection:
    canonical_label: str
    conf: float
    box: tuple[int, int, int, int]
    role: str = "primary"


@dataclass
class SceneHand:
    handedness: str
    box: tuple[int, int, int, int]
    palm_center: tuple[float, float]
    is_gripping: bool


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


def prepare_context():
    recipe = load_recipe("sop/tomato_egg.json")
    step = next(s for s in recipe.steps if s.id == "step_01_prepare")
    context = SessionContext(
        session_id="ses_test",
        recipe_version_id=recipe.recipe_version_id,
        current_step_id=step.id,
        started_at=datetime(2026, 7, 23, tzinfo=UTC),
        active_objects=step.objects_involved,
        context_version=1,
    )
    return recipe, context


def test_stovetop_wok_suppresses_overlapping_bowl_reading() -> None:
    # Chest-cam failure mode: the wok on the stove fires as "bowl". With wok
    # prompted as a bowl confuser, the overlapping wok candidate must win
    # even when the bowl conf is slightly higher.
    recipe, context = prepare_context()
    detection_context = build_detection_context(context, recipe)
    raw = [
        FakeDetection("bowl", 0.58, (100, 100, 420, 380)),
        FakeDetection("wok", 0.51, (95, 105, 430, 390)),
        # the real egg bowl elsewhere in frame: no wok overlap, must survive
        FakeDetection("small mixing bowl", 0.61, (600, 200, 760, 330)),
    ]
    normalized = canonicalize_detections(raw, detection_context)
    labels = [(d.canonical_label, d.box) for d in normalized]
    assert ("wok", (95, 105, 430, 390)) in labels
    assert ("bowl", (600, 200, 760, 330)) in labels
    assert ("bowl", (100, 100, 420, 380)) not in labels


def test_confident_real_bowl_is_not_suppressed_by_weak_wok() -> None:
    # A genuine bowl keeps a large conf gap over the "wok" prompt; the
    # suppression margin must leave it alone.
    recipe, context = prepare_context()
    detection_context = build_detection_context(context, recipe)
    raw = [
        FakeDetection("bowl", 0.80, (100, 100, 420, 380)),
        FakeDetection("wok", 0.50, (95, 105, 430, 390)),
    ]
    normalized = canonicalize_detections(raw, detection_context)
    assert {d.canonical_label for d in normalized} == {"bowl", "wok"}


def test_vlm_curation_dedupes_and_keeps_only_primary_boxes() -> None:
    low = SceneDetection("tomato", 0.55, (0, 0, 10, 10))
    high = SceneDetection("tomato", 0.81, (10, 10, 30, 30))
    faint = SceneDetection("bowl", 0.49, (40, 40, 60, 60))
    anchor = SceneDetection("egg", 0.99, (70, 70, 90, 90), role="anchor")

    curated = curate_detections([low, high, faint, anchor])

    assert curated.confident == [("tomato", 0.81)]
    assert curated.faint == [("bowl", 0.49)]
    assert confident_detection_items([low, high, faint, anchor]) == [high]


def test_vlm_curation_floor_is_inclusive() -> None:
    at_floor = SceneDetection("egg", 0.30, (0, 0, 10, 10))
    below_floor = SceneDetection("kitchen_knife", 0.299, (20, 0, 30, 10))

    curated = curate_detections(
        [at_floor, below_floor],
        expected_objects=("egg", "kitchen_knife"),
    )

    assert curated.faint == [("egg", 0.30)]
    assert curated.missing_expected == ["kitchen_knife"]


def test_vlm_curation_normalizes_missing_expected_names() -> None:
    detected = SceneDetection("soy_sauce-bottle", 0.30, (0, 0, 10, 10))

    curated = curate_detections(
        [detected],
        expected_objects=("soy sauce bottle", "oil-bottle"),
    )

    assert curated.missing_expected == ["oil-bottle"]


def test_scene_context_has_spatial_hand_hint_but_drops_low_confidence_relation() -> None:
    hand = SceneHand(
        handedness="Right",
        box=(80, 80, 180, 180),
        palm_center=(130.0, 130.0),
        is_gripping=True,
    )
    tomato = SceneDetection("tomato", 0.75, (20, 220, 120, 320))
    knife = SceneDetection("kitchen_knife", 0.80, (110, 110, 160, 150))

    context = format_scene_context(
        [tomato, knife],
        [hand],
        (900, 600),
        ("tomato", "kitchen_knife"),
    )
    assert "右手拿着菜刀" in context
    assert "番茄在画面左侧" in context

    low_context = format_scene_context(
        [tomato, SceneDetection("kitchen_knife", 0.23, knife.box)],
        [hand],
        (900, 600),
        ("tomato", "kitchen_knife"),
    )
    assert "右手拿着菜刀" not in low_context
    assert "本步应出现但没框到：菜刀" in low_context
