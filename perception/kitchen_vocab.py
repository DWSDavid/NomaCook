"""Kitchen vocabulary for YOLO-World open-vocabulary detection.

Track C (runbook Step C1). YOLO-World is zero-training: the vocabulary list
IS the dataset lever. This module is the single source of truth for kitchen
object words — detector.py and the harnesses import from here.

All terms are English (YOLO-World's text encoder is English CLIP). The SOP
schema stores English `objects_involved`; `vocab_for_step()` expands them
with synonyms to boost recall.

Curation rules:
- Grounded in the 10 corpus dishes (sop/corpus/): fried-rice-class stir-fry.
- Prefer visually distinctive phrasings ("soy sauce bottle" over "soy sauce").
- Don't dump every word into one detect() call — more classes = lower
  per-class precision and slower text encoding. Use per-step vocab in
  production; the full list is for offline eval (harness/eval_vocab.py).
"""

from __future__ import annotations

# fmt: off
KITCHEN_VOCAB: dict[str, list[str]] = {
    # Things food gets cooked in/on
    "cookware": [
        "wok", "frying pan", "pot", "pot lid",
    ],
    # Hand-held tools
    "utensils": [
        "spatula", "ladle", "kitchen knife", "chopsticks", "spoon",
        "peeler", "scissors",
    ],
    # Things food sits in
    "containers": [
        "bowl", "plate", "cup", "cutting board", "colander",
    ],
    # Bottled/jarred condiments — bottle-level phrasing detects far better
    # than the condiment itself
    "condiments": [
        "soy sauce bottle", "oil bottle", "vinegar bottle",
        "salt shaker", "seasoning jar", "cola can",
    ],
    # Raw ingredients for the 10 corpus dishes
    "ingredients": [
        "egg", "rice", "scallion", "garlic", "ginger",
        "tomato", "potato", "spinach", "cabbage", "lotus root",
        "instant noodles", "ham", "carrot",
    ],
    # Fixed equipment (ROI anchors / scene context)
    "appliances": [
        "induction cooktop", "gas stove", "rice cooker", "range hood",
        "sink", "faucet",
    ],
    # Hand-object interaction (MediaPipe is the primary hand signal;
    # YOLO "hand" is a cheap cross-check)
    "hands": [
        "hand",
    ],
}
# fmt: on

# Recall boosters: when a step's objects_involved names the key, also prompt
# the synonyms. YOLO-World often fires on one phrasing but not another.
SYNONYMS: dict[str, list[str]] = {
    "wok": ["frying pan"],
    "frying pan": ["wok"],
    "bottle": ["soy sauce bottle", "oil bottle"],
    "knife": ["kitchen knife"],
    "cooktop": ["induction cooktop", "gas stove"],
    "noodles": ["instant noodles"],
}


def full_vocab() -> list[str]:
    """Every term across categories, deduped, order-stable. For offline eval."""
    seen: dict[str, None] = {}
    for terms in KITCHEN_VOCAB.values():
        for term in terms:
            seen.setdefault(term, None)
    return list(seen)


def category_of(term: str) -> str | None:
    for category, terms in KITCHEN_VOCAB.items():
        if term in terms:
            return category
    return None


def vocab_for_step(objects_involved: list[str]) -> list[str]:
    """Expand a step's objects_involved into a detection vocabulary.

    Adds synonyms plus the always-on anchors (cookware for ROI locking,
    hand for interaction) so the per-step prompt stays small but never
    loses the pan or the hands.
    """
    seen: dict[str, None] = {}
    for obj in objects_involved:
        seen.setdefault(obj, None)
        for syn in SYNONYMS.get(obj, []):
            seen.setdefault(syn, None)
    for anchor in KITCHEN_VOCAB["cookware"] + KITCHEN_VOCAB["hands"]:
        seen.setdefault(anchor, None)
    return list(seen)
