"""Minimal runtime config loader for NomaCook domain packs.

Each domain pack YAML controls the perception layer for one task. The
StateEngine still owns completion criteria, weights, and state advancement
via sop/*.json. This module only reads perception knobs: vocabulary,
detector params, regions, and step-aware object lists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class DomainConfig:
    task_id: str
    dish: str

    vocab: list[str]
    canonical_map: dict[str, list[str]]

    detector_device: str
    detector_conf: float
    detect_every: int
    stability_frames: int

    table_fraction: float
    fridge_fallback: tuple[int, int, int, int]  # x1,y1,x2,y2 in [0,1] fraction

    step_objects: dict[str, list[str]]  # step_id → list of canonical labels to detect

    @classmethod
    def load(cls, path: str | Path) -> "DomainConfig":
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        p = raw["perception"]
        regions = raw["regions"]
        fb = regions["refrigerator_interior"]["fallback_box"]

        # vocab: flatten [list, ...] entries
        vocab_raw = raw["vocab"]
        all_terms: list[str] = []
        for group in vocab_raw.values():
            if isinstance(group, list):
                all_terms.extend(group)
        all_terms = list(dict.fromkeys(all_terms))  # dedup, order-stable

        step_objs: dict[str, list[str]] = {}
        for step_id, cfg in raw.get("steps", {}).items():
            step_objs[step_id] = list(cfg.get("objects_detect", []))

        return cls(
            task_id=raw["task_id"],
            dish=raw["dish"],
            vocab=all_terms,
            canonical_map=raw.get("canonical_map", {}),
            detector_device=p.get("detector_device", "mps"),
            detector_conf=p.get("detector_conf", 0.10),
            detect_every=p.get("detect_every", 3),
            stability_frames=p.get("stability_frames", 3),
            table_fraction=regions["table"].get("fraction", 0.70),
            fridge_fallback=(
                fb["x1"], fb["y1"], fb["x2"], fb["y2"],
            ),
            step_objects=step_objs,
        )

    def vocab_for_step(self, step_id: str) -> list[str]:
        """YOLO vocabulary for a step: step objects + always-on anchors."""
        objects = self.step_objects.get(step_id, [])
        anchors = ["hand"]
        # expand aliases to prompts
        terms: list[str] = []
        seen: set[str] = set()
        for obj in objects:
            aliases = self.canonical_map.get(obj, [obj])
            for a in aliases:
                if a not in seen:
                    terms.append(a)
                    seen.add(a)
        for a in anchors:
            if a not in seen:
                terms.append(a)
                seen.add(a)
        return terms

    def canonicalize(self, detections: list[tuple[str, float, tuple[int, int, int, int]]]) -> list[tuple[str, float, tuple[int, int, int, int]]]:
        """Dedup overlapping aliases, keep highest-confidence per canonical label."""
        best: dict[str, tuple[float, tuple[int, int, int, int]]] = {}
        reverse_map: dict[str, str] = {}
        for canon, aliases in self.canonical_map.items():
            for a in aliases:
                reverse_map[a] = canon
        for label, conf, box in detections:
            canonical = reverse_map.get(label)
            if canonical is None:
                continue
            if canonical not in best or conf > best[canonical][0]:
                best[canonical] = (conf, box)
        return [(label, conf, box) for label, (conf, box) in best.items()]
