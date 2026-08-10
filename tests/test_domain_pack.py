"""Verify domain_packs/kitchen/tomato_to_fridge.yaml is a valid runtime config.

Focuses on detecting runtime drift: missing steps, broken canonical maps,
orphaned object names that can't produce detector prompts.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from server.domain.config import DomainConfig
from server.engine import load_recipe


REPO = Path(__file__).resolve().parent.parent
YAML_PATH = REPO / "domain_packs" / "kitchen" / "tomato_to_fridge.yaml"


def test_domain_config_loads() -> None:
    cfg = DomainConfig.load(YAML_PATH)
    assert cfg.task_id == "tomato_to_fridge_v1"
    assert "tomato" in cfg.vocab
    assert cfg.canonical_map


def test_step_objects_cover_all_sop_steps() -> None:
    recipe = load_recipe(REPO / "sop" / "tomato_to_fridge.json")
    cfg = DomainConfig.load(YAML_PATH)
    sop_ids = {step.id for step in recipe.steps}
    cfg_ids = set(cfg.step_objects.keys())
    assert cfg_ids == sop_ids, f"mismatch: SOP={sop_ids - cfg_ids}, YAML={cfg_ids - sop_ids}"


def test_all_step_objects_have_canonical_aliases() -> None:
    cfg = DomainConfig.load(YAML_PATH)
    all_canonical = set(cfg.canonical_map.keys())
    for step_id, objs in cfg.step_objects.items():
        for obj in objs:
            assert obj in all_canonical, (
                f"step {step_id}: object {obj!r} not in canonical_map"
            )


def test_vocab_for_step_produces_non_empty_prompts() -> None:
    cfg = DomainConfig.load(YAML_PATH)
    for step_id in cfg.step_objects:
        prompts = cfg.vocab_for_step(step_id)
        assert len(prompts) > 0, f"step {step_id}: empty vocabulary"
        assert any("tomato" in p for p in prompts), (
            f"step {step_id}: no tomato-related prompt in {prompts}"
        )


def test_canonical_map_includes_self() -> None:
    cfg = DomainConfig.load(YAML_PATH)
    for canon, aliases in cfg.canonical_map.items():
        assert canon in aliases, (
            f"canonical label {canon!r} must include itself in aliases"
        )


def test_no_overlapping_canonical_aliases() -> None:
    cfg = DomainConfig.load(YAML_PATH)
    seen: dict[str, str] = {}
    for canon, aliases in cfg.canonical_map.items():
        for a in aliases:
            if a in seen and seen[a] != canon:
                raise AssertionError(
                    f"alias {a!r} maps to both {seen[a]!r} and {canon!r}"
                )
            seen[a] = canon


def test_yaml_matches_loaded_config() -> None:
    """Verify DomainConfig.load() round-trips key values from YAML."""
    cfg = DomainConfig.load(YAML_PATH)
    with YAML_PATH.open() as f:
        raw = yaml.safe_load(f)
    p = raw["perception"]
    assert cfg.detector_conf == p["detector_conf"]
    assert cfg.detect_every == p["detect_every"]
    assert cfg.stability_frames == p["stability_frames"]
    assert cfg.table_fraction == raw["regions"]["table"]["fraction"]
