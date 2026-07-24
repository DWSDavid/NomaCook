"""Demo-facing terminal ticker: clean, tick-marked lines for a pitch audience.

Separate from the engineer-facing stdout in run_pipeline. Presentation only —
it derives nothing, just formats what the pipeline already knows. Wire it at a
few existing call sites (see docs/HANDOFF-demo-log-and-dish.md). Toggle with
--demo-log so the noisy engineer log stays available for debugging.

Design: idempotent prints. Feed it the current facts each keyframe; it only
emits a line when something actually changed, so the terminal reads like a
running checklist, not a firehose.
"""

from __future__ import annotations

# Chinese display names for canonical detection labels + signal states.
LABEL_ZH: dict[str, str] = {
    "tomato": "番茄", "egg": "鸡蛋", "bowl": "碗", "plate": "盘子",
    "wok": "炒锅", "frying pan": "炒锅", "spatula": "木铲", "ladle": "汤勺",
    "kitchen_knife": "菜刀", "chopsticks": "筷子", "cutting_board": "砧板",
    "oil_bottle": "油瓶", "soy_sauce_bottle": "酱油瓶",
    "vinegar_bottle": "醋瓶", "salt": "盐", "sugar": "糖",
    "scallion": "葱", "hand": "手",
}

_BAR_FULL = "▰"
_BAR_EMPTY = "▱"


def label_zh(label: str) -> str:
    return LABEL_ZH.get(label, label)


def _bar(score: float, threshold: float, width: int = 6) -> str:
    filled = min(width, max(0, round(score / max(threshold, 1e-6) * width)))
    return _BAR_FULL * filled + _BAR_EMPTY * (width - filled)


class DemoLogger:
    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self._last_labels: tuple[str, ...] = ()
        self._spoken_signals: set[str] = set()

    def _p(self, line: str) -> None:
        if self.enabled:
            print(line)

    def step_enter(self, *, sequence: int, total: int, title: str) -> None:
        self._last_labels = ()
        self._spoken_signals = set()
        self._p(f"\n▶  第 {sequence} 步 / 共 {total} 步   {title}")

    def detections(self, labels: list[str]) -> None:
        """Print a ✓ line only when the visible set changes."""
        uniq = tuple(sorted(set(labels)))
        if not uniq or uniq == self._last_labels:
            return
        self._last_labels = uniq
        shown = "、".join(label_zh(label) for label in uniq)
        self._p(f"   ✓ 看到:{shown}")

    def signal(self, text: str) -> None:
        """A one-off confirmed signal, e.g. '手拿着菜刀'. Deduped per step."""
        if text in self._spoken_signals:
            return
        self._spoken_signals.add(text)
        self._p(f"   ✓ {text}")

    def vlm(self, phase: str, confidence: float, reason: str) -> None:
        phase_zh = {"not_started": "还没开始", "in_progress": "进行中",
                    "likely_complete": "看起来快好了"}.get(phase, phase)
        tail = f"  ({reason.strip()})" if reason else ""
        self._p(f"   🔍 Gemini:{phase_zh}{tail}  信心 {confidence:.0%}")

    def remark(self, text: str) -> None:
        self._p(f"   🔥 Gemini 提醒:{text}")

    def score(self, score: float, threshold: float, *, hit: bool) -> None:
        flag = "  达标!" if hit else ""
        self._p(f"   进度 {_bar(score, threshold)}  "
                f"{score:.2f} / {threshold:.2f}{flag}")

    def step_done(self, next_instruction: str | None) -> None:
        if next_instruction:
            self._p(f"   ✅ 完成 → 下一步:{next_instruction}")
        else:
            self._p("   ✅ 完成")

    def dish(self, name: str) -> None:
        self._p(f"\n🍽  {name}做好了 ✓   「妈，我会做饭了」")
