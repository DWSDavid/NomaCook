"""Build a small, step-aware YOLO vocabulary from the current SessionContext."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Literal, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field

from server.engine.models import SessionContext
from server.engine.sop import RecipeSOP


TargetRole = Literal["primary", "anchor", "confuser"]
Category = Literal[
    "cookware",
    "utensil",
    "container",
    "condiment",
    "ingredient",
    "appliance",
    "hand",
    "unknown",
]


class ObjectConcept(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    canonical_label: str
    prompts: tuple[str, ...] = Field(min_length=1)
    aliases: tuple[str, ...] = ()
    category: Category
    min_confidence: float = Field(ge=0.0, le=1.0)


def _concept(
    canonical_label: str,
    prompts: tuple[str, ...],
    category: Category,
    min_confidence: float,
    aliases: tuple[str, ...] = (),
) -> ObjectConcept:
    return ObjectConcept(
        canonical_label=canonical_label,
        prompts=prompts,
        aliases=aliases,
        category=category,
        min_confidence=min_confidence,
    )


CONCEPTS: tuple[ObjectConcept, ...] = (
    _concept("wok", ("wok", "frying pan"), "cookware", 0.16, ("pan",)),
    _concept("pot", ("cooking pot", "saucepan"), "cookware", 0.18),
    _concept("pot_lid", ("pot lid",), "cookware", 0.18),
    _concept("spatula", ("cooking spatula", "turner spatula"), "utensil", 0.16),
    _concept("ladle", ("cooking ladle",), "utensil", 0.18),
    _concept("kitchen_knife", ("kitchen knife", "chef knife"), "utensil", 0.18, ("knife",)),
    _concept("chopsticks", ("chopsticks",), "utensil", 0.18),
    _concept("spoon", ("cooking spoon",), "utensil", 0.18),
    _concept("peeler", ("vegetable peeler",), "utensil", 0.20),
    _concept("scissors", ("kitchen scissors",), "utensil", 0.20),
    _concept("bowl", ("mixing bowl", "bowl"), "container", 0.16),
    _concept("plate", ("dinner plate", "plate"), "container", 0.18),
    _concept("cup", ("drinking cup", "cup"), "container", 0.18),
    _concept("cutting_board", ("cutting board",), "container", 0.16),
    _concept("colander", ("kitchen colander",), "container", 0.20),
    _concept(
        "soy_sauce_bottle",
        ("soy sauce bottle", "dark condiment bottle"),
        "condiment",
        0.15,
        ("soy sauce",),
    ),
    _concept("oil_bottle", ("cooking oil bottle", "oil bottle"), "condiment", 0.15, ("oil",)),
    _concept("vinegar_bottle", ("vinegar bottle",), "condiment", 0.16, ("vinegar",)),
    _concept("salt", ("salt shaker", "salt container"), "condiment", 0.18),
    _concept("pepper", ("pepper shaker", "pepper container"), "condiment", 0.18),
    _concept("seasoning_jar", ("seasoning jar", "spice jar"), "condiment", 0.18),
    _concept("cola_can", ("cola can", "soda can"), "condiment", 0.18),
    _concept("egg", ("chicken egg", "egg"), "ingredient", 0.16),
    _concept("rice", ("cooked rice", "rice"), "ingredient", 0.18),
    _concept("scallion", ("green onion", "scallion"), "ingredient", 0.18, ("spring onion",)),
    _concept("garlic", ("garlic clove", "garlic"), "ingredient", 0.18),
    _concept("ginger", ("ginger root", "ginger"), "ingredient", 0.18),
    _concept("tomato", ("tomato",), "ingredient", 0.16),
    _concept("potato", ("potato",), "ingredient", 0.16),
    _concept("spinach", ("spinach leaves", "spinach"), "ingredient", 0.18),
    _concept("cabbage", ("cabbage",), "ingredient", 0.17),
    _concept("lotus_root", ("lotus root", "sliced lotus root"), "ingredient", 0.20),
    _concept("instant_noodles", ("instant noodles", "noodle block"), "ingredient", 0.18, ("noodles",)),
    _concept("ham", ("diced ham", "ham"), "ingredient", 0.19),
    _concept("carrot", ("carrot", "diced carrot"), "ingredient", 0.16),
    _concept("cucumber", ("cucumber", "diced cucumber"), "ingredient", 0.17),
    _concept("induction_cooktop", ("induction cooktop",), "appliance", 0.18, ("cooktop",)),
    _concept("gas_stove", ("gas stove",), "appliance", 0.18, ("stove",)),
    _concept("rice_cooker", ("rice cooker",), "appliance", 0.18),
    _concept("range_hood", ("range hood",), "appliance", 0.20),
    _concept("sink", ("kitchen sink",), "appliance", 0.18),
    _concept("faucet", ("kitchen faucet",), "appliance", 0.18),
    _concept("hand", ("human hand",), "hand", 0.25),
)


def _normal_form(value: str) -> str:
    return re.sub(r"[\s_-]+", " ", value.strip().lower())


_CONCEPT_BY_NAME: dict[str, ObjectConcept] = {}
for _item in CONCEPTS:
    for _name in (_item.canonical_label, *_item.prompts, *_item.aliases):
        _CONCEPT_BY_NAME[_normal_form(_name)] = _item


class DetectionTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    canonical_label: str
    prompts: tuple[str, ...] = Field(min_length=1)
    category: Category
    min_confidence: float = Field(ge=0.0, le=1.0)
    role: TargetRole


class DetectionContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    recipe_version_id: str
    step_id: str
    context_version: int
    targets: tuple[DetectionTarget, ...]

    @property
    def prompts(self) -> tuple[str, ...]:
        return tuple(prompt for target in self.targets for prompt in target.prompts)

    @property
    def prompt_to_target(self) -> dict[str, DetectionTarget]:
        return {
            _normal_form(prompt): target
            for target in self.targets
            for prompt in target.prompts
        }


BOTTLE_CONFUSERS: dict[str, tuple[str, ...]] = {
    "soy_sauce_bottle": ("oil_bottle", "vinegar_bottle"),
    "oil_bottle": ("soy_sauce_bottle", "vinegar_bottle"),
    "vinegar_bottle": ("soy_sauce_bottle", "oil_bottle"),
}


def _unknown_concept(value: str) -> ObjectConcept:
    canonical = _normal_form(value).replace(" ", "_")
    return _concept(canonical, (_normal_form(value),), "unknown", 0.20)


def resolve_concept(value: str) -> ObjectConcept:
    return _CONCEPT_BY_NAME.get(_normal_form(value), _unknown_concept(value))


def _target(concept: ObjectConcept, role: TargetRole) -> DetectionTarget:
    return DetectionTarget(
        canonical_label=concept.canonical_label,
        prompts=concept.prompts,
        category=concept.category,
        min_confidence=concept.min_confidence,
        role=role,
    )


def _fit_prompt_budget(
    targets: Sequence[DetectionTarget], max_prompts: int
) -> tuple[DetectionTarget, ...]:
    if max_prompts < 1:
        raise ValueError("max_prompts must be positive")
    primary_count = sum(target.role == "primary" for target in targets)
    if primary_count > max_prompts:
        raise ValueError("max_prompts is too small for all primary step objects")

    selected_prompts: dict[str, list[str]] = {
        target.canonical_label: [] for target in targets
    }
    remaining = max_prompts

    # Every current-step object gets one class before aliases or diagnostics.
    for target in targets:
        if target.role == "primary":
            selected_prompts[target.canonical_label].append(target.prompts[0])
            remaining -= 1

    # Then add one cheap anchor/confuser class when budget allows.
    for role in ("anchor", "confuser"):
        for target in targets:
            if remaining <= 0:
                break
            if target.role == role:
                selected_prompts[target.canonical_label].append(target.prompts[0])
                remaining -= 1

    # Finally distribute alternate phrasings without starving later targets.
    alias_index = 1
    while remaining > 0:
        added = False
        for role in ("primary", "anchor", "confuser"):
            for target in targets:
                if remaining <= 0:
                    break
                if target.role == role and alias_index < len(target.prompts):
                    selected_prompts[target.canonical_label].append(
                        target.prompts[alias_index]
                    )
                    remaining -= 1
                    added = True
        if not added:
            break
        alias_index += 1

    return tuple(
        target.model_copy(
            update={"prompts": tuple(selected_prompts[target.canonical_label])}
        )
        for target in targets
        if selected_prompts[target.canonical_label]
    )


def build_detection_context(
    context: SessionContext,
    recipe: RecipeSOP,
    *,
    include_bottle_confusers: bool = True,
    max_prompts: int = 18,
) -> DetectionContext:
    if context.recipe_version_id != recipe.recipe_version_id:
        raise ValueError("SessionContext and SOP recipe versions do not match")
    if context.current_step_id not in {step.id for step in recipe.steps}:
        raise ValueError(f"unknown current step {context.current_step_id!r}")

    targets: list[DetectionTarget] = []
    seen: set[str] = set()
    for raw_object in context.active_objects:
        concept = resolve_concept(raw_object)
        if concept.canonical_label not in seen:
            targets.append(_target(concept, "primary"))
            seen.add(concept.canonical_label)

    hand = resolve_concept("hand")
    if hand.canonical_label not in seen:
        targets.append(_target(hand, "anchor"))
        seen.add(hand.canonical_label)

    if include_bottle_confusers:
        primary_labels = tuple(target.canonical_label for target in targets)
        for primary_label in primary_labels:
            for confuser_label in BOTTLE_CONFUSERS.get(primary_label, ()):
                concept = resolve_concept(confuser_label)
                if concept.canonical_label not in seen:
                    targets.append(_target(concept, "confuser"))
                    seen.add(concept.canonical_label)

    return DetectionContext(
        recipe_version_id=recipe.recipe_version_id,
        step_id=context.current_step_id,
        context_version=context.context_version,
        targets=_fit_prompt_budget(targets, max_prompts),
    )


class VocabularyDetector(Protocol):
    def set_vocab(self, vocab: list[str]) -> None: ...


class ContextualVocabularyController:
    """Update YOLO classes only when the effective prompt set changes."""

    def __init__(self, detector: VocabularyDetector) -> None:
        self.detector = detector
        self._last_prompts: tuple[str, ...] = ()

    def sync(self, detection_context: DetectionContext) -> bool:
        prompts = detection_context.prompts
        if prompts == self._last_prompts:
            return False
        self.detector.set_vocab(list(prompts))
        self._last_prompts = prompts
        return True


class RawDetection(Protocol):
    label: str
    conf: float
    box: tuple[int, int, int, int]


@dataclass(frozen=True)
class ContextDetection:
    canonical_label: str
    prompt: str
    conf: float
    box: tuple[int, int, int, int]
    role: TargetRole


def _iou(
    first: tuple[int, int, int, int], second: tuple[int, int, int, int]
) -> float:
    x1 = max(first[0], second[0])
    y1 = max(first[1], second[1])
    x2 = min(first[2], second[2])
    y2 = min(first[3], second[3])
    intersection = max(0, x2 - x1) * max(0, y2 - y1)
    if intersection == 0:
        return 0.0
    first_area = max(0, first[2] - first[0]) * max(0, first[3] - first[1])
    second_area = max(0, second[2] - second[0]) * max(0, second[3] - second[1])
    union = first_area + second_area - intersection
    return intersection / union if union else 0.0


def canonicalize_detections(
    detections: Sequence[RawDetection],
    detection_context: DetectionContext,
    *,
    alias_iou_threshold: float = 0.60,
) -> list[ContextDetection]:
    """Apply per-concept thresholds and merge overlapping synonym predictions."""

    if not 0.0 <= alias_iou_threshold <= 1.0:
        raise ValueError("alias_iou_threshold must be between 0 and 1")
    mapping = detection_context.prompt_to_target
    candidates: list[ContextDetection] = []
    for detection in detections:
        target = mapping.get(_normal_form(detection.label))
        if target is None or detection.conf < target.min_confidence:
            continue
        candidates.append(
            ContextDetection(
                canonical_label=target.canonical_label,
                prompt=detection.label,
                conf=float(detection.conf),
                box=tuple(detection.box),
                role=target.role,
            )
        )

    kept: list[ContextDetection] = []
    for candidate in sorted(candidates, key=lambda item: item.conf, reverse=True):
        if any(
            existing.canonical_label == candidate.canonical_label
            and _iou(existing.box, candidate.box) >= alias_iou_threshold
            for existing in kept
        ):
            continue
        kept.append(candidate)
    return kept
