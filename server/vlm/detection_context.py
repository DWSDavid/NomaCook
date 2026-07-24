"""Curate the noisy local-detector output into a clean hint for the VLM.

The raw YOLO-World overlay is deliberately busy (duplicate eggs, three bowls,
wok read as bowl, knife at 0.23). Feeding that verbatim to Gemini adds noise.
This module dedupes per label, drops low-confidence junk, and formats a short
Chinese context block that says what is confidently visible, what is faint,
and which of THIS step's expected objects were not found.

Design rule (keeps the fusion moat intact): this is CONTEXT for the VLM, not a
vote. The VLM still judges independently and is told to trust its own eyes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Protocol

# Chinese display names. Superset of what shows on screen.
_LABEL_ZH: dict[str, str] = {
    "tomato": "番茄", "egg": "鸡蛋", "bowl": "碗", "plate": "盘子",
    "wok": "炒锅", "pot": "锅", "spatula": "木铲", "ladle": "汤勺",
    "kitchen_knife": "菜刀", "chopsticks": "筷子", "cutting_board": "砧板",
    "oil_bottle": "油瓶", "soy_sauce_bottle": "酱油瓶", "vinegar_bottle": "醋瓶",
    "salt": "盐", "scallion": "葱", "garlic": "蒜", "hand": "手",
    "induction_cooktop": "电磁炉", "gas_stove": "灶台",
}


def zh(label: str) -> str:
    key = _norm(label).replace(" ", "_")
    return _LABEL_ZH.get(key, label.replace("_", " "))


def _norm(value: str) -> str:
    return re.sub(r"[\s_-]+", " ", value.strip().lower())


class _Det(Protocol):
    canonical_label: str
    conf: float


@dataclass
class CuratedDetections:
    confident: list[tuple[str, float]]  # (canonical_label, conf), desc
    faint: list[tuple[str, float]]
    missing_expected: list[str]         # canonical labels expected but unseen

    @property
    def is_empty(self) -> bool:
        return not self.confident and not self.faint


def curate_detections(
    detections: Iterable[_Det],
    expected_objects: Iterable[str] = (),
    *,
    floor: float = 0.30,
    confident_at: float = 0.50,
) -> CuratedDetections:
    """Dedupe per label (keep max conf), split confident vs faint, and flag
    expected objects that were not detected above the floor."""
    best: dict[str, float] = {}
    for det in detections:
        if getattr(det, "role", "primary") != "primary":
            continue
        label = det.canonical_label
        best[label] = max(best.get(label, 0.0), float(det.conf))

    confident = sorted(
        ((l, c) for l, c in best.items() if c >= confident_at),
        key=lambda item: -item[1],
    )
    faint = sorted(
        ((l, c) for l, c in best.items() if floor <= c < confident_at),
        key=lambda item: -item[1],
    )

    seen_norm = {_norm(l): c for l, c in best.items()}
    missing = [
        obj for obj in expected_objects
        if seen_norm.get(_norm(obj), 0.0) < floor
    ]
    return CuratedDetections(confident=confident, faint=faint, missing_expected=missing)


def confident_detection_items(
    detections,
    expected_objects: Iterable[str] = (),
    *,
    floor: float = 0.30,
    confident_at: float = 0.50,
) -> list[_Det]:
    """Return one highest-confidence primary detection per confident label."""
    items = list(detections)
    curated = curate_detections(
        items,
        expected_objects,
        floor=floor,
        confident_at=confident_at,
    )
    wanted = {label for label, _ in curated.confident}
    best: dict[str, _Det] = {}
    for det in items:
        label = det.canonical_label
        if label not in wanted or getattr(det, "role", "primary") != "primary":
            continue
        current = best.get(label)
        if current is None or float(det.conf) > float(current.conf):
            best[label] = det
    return sorted(best.values(), key=lambda det: -float(det.conf))


def format_detection_context(curated: CuratedDetections) -> str:
    """Render the curated set as a short Chinese prompt block for the VLM."""
    if curated.is_empty:
        return "本地检测器在这一帧没有稳定框出任何物体。"

    lines = [
        "以下是本地检测器在这一帧自动框出的物体（可能有重复、误标或漏检，"
        "仅供参考，以你自己看到的画面为准）："
    ]
    if curated.confident:
        names = "、".join(zh(l) for l, _ in curated.confident)
        lines.append(f"- 较确定：{names}")
    if curated.faint:
        names = "、".join(zh(l) for l, _ in curated.faint)
        lines.append(f"- 不太确定：{names}")
    if curated.missing_expected:
        names = "、".join(zh(l) for l in curated.missing_expected)
        lines.append(f"- 本步应出现但没框到：{names}")
    return "\n".join(lines)


# ---------------------------------------------------------------- spatial

class _Hand(Protocol):
    handedness: str
    box: tuple[int, int, int, int]
    palm_center: tuple[float, float]
    is_gripping: bool


def _zone_zh(box: tuple[int, int, int, int], w: int, h: int) -> str:
    cx = (box[0] + box[2]) / 2 / max(w, 1)
    cy = (box[1] + box[3]) / 2 / max(h, 1)
    vert = "上" if cy < 0.38 else ("下" if cy > 0.62 else "")
    horiz = "左" if cx < 0.38 else ("右" if cx > 0.62 else "中")
    if vert and horiz != "中":
        return f"{horiz}{vert}方"
    if vert:
        return f"画面{vert}方"
    if horiz == "中":
        return "画面中间"
    return f"画面{horiz}侧"


# You don't "hold" a surface. These downgrade to a "near" relation.
_SURFACES = frozenset({
    "cutting_board", "wok", "pot", "induction_cooktop", "gas_stove",
    "sink", "colander", "range_hood", "faucet", "rice_cooker",
})


def _area(box: tuple[int, int, int, int]) -> int:
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def _hand_relations(detections, hands) -> list[str]:
    from perception.fusion import classify_interaction

    out: list[str] = []
    for hand in hands:
        hand_zh = "右手" if hand.handedness == "Right" else "左手"
        holds: list[tuple[int, str]] = []
        nears: list[tuple[int, str]] = []
        for det in detections:
            rel = classify_interaction(
                hand.palm_center, hand.box, hand.is_gripping, det.box
            )
            if rel is None:
                continue
            entry = (_area(det.box), det.canonical_label)
            if rel == "holding" and det.canonical_label not in _SURFACES:
                holds.append(entry)
            else:
                nears.append(entry)
        if holds:  # you grip small things: prefer the smallest candidate
            out.append(f"{hand_zh}拿着{zh(min(holds)[1])}")
        elif nears:
            out.append(f"{hand_zh}在{zh(min(nears)[1])}附近")
    return out


def format_scene_context(
    detections,
    hands,
    frame_wh: tuple[int, int],
    expected_objects: Iterable[str] = (),
    *,
    floor: float = 0.30,
) -> str:
    """Richer than a label list: hand-object relations + coarse positions.

    This is the high-value context. A bare "there is a tomato" is something the
    VLM already sees; "右手拿着菜刀、番茄在砧板上" is what it cannot reliably
    infer from one frame and what actually informs the completion judgment.
    """
    detections = list(detections)
    w, h = frame_wh
    curated = curate_detections(detections, expected_objects, floor=floor)
    if curated.is_empty and not hands:
        return "本地检测器在这一帧没有稳定框出任何物体。"

    stable = confident_detection_items(
        detections,
        expected_objects,
        floor=floor,
        confident_at=floor,
    )

    lines = [
        "以下是本地感知的粗略估计（位置和手部关系，可能有误，"
        "仅供参考，以你自己看到的画面为准）："
    ]
    relations = _hand_relations(stable, hands)
    if relations:
        lines.append(f"- 手部：{'；'.join(relations)}")

    # Coarse positions, but only for this step's relevant objects (skip the
    # pantry clutter in the background), highest-confidence box per label,
    # capped so the line stays short.
    relevant = {_norm(o) for o in expected_objects} or None
    best_box: dict[str, tuple[int, int, int, int]] = {}
    best_conf: dict[str, float] = {}
    for det in confident_detection_items(detections, expected_objects):
        if relevant is not None and _norm(det.canonical_label) not in relevant:
            continue
        if det.conf > best_conf.get(det.canonical_label, 0.0):
            best_conf[det.canonical_label] = det.conf
            best_box[det.canonical_label] = det.box
    ranked = sorted(best_box, key=lambda l: -best_conf[l])[:5]
    if ranked:
        pos = "，".join(f"{zh(l)}在{_zone_zh(best_box[l], w, h)}" for l in ranked)
        lines.append(f"- 位置：{pos}")
    if curated.missing_expected:
        names = "、".join(zh(l) for l in curated.missing_expected)
        lines.append(f"- 本步应出现但没框到：{names}")
    return "\n".join(lines)
