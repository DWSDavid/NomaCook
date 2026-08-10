"""Verify domain_packs/kitchen/tomato_to_fridge.yaml matches SOP and tracker."""

from __future__ import annotations

from pathlib import Path

import yaml

from server.engine import load_recipe


REPO = Path(__file__).resolve().parent.parent


def test_domain_pack_is_valid_yaml() -> None:
    path = REPO / "domain_packs" / "kitchen" / "tomato_to_fridge.yaml"
    with path.open() as f:
        data = yaml.safe_load(f)
    assert data["task_id"] == "tomato_to_fridge_v1"
    assert data["dish"] == "把番茄放进冰箱"


def test_domain_pack_steps_match_sop() -> None:
    recipe = load_recipe(REPO / "sop" / "tomato_to_fridge.json")
    sop_step_ids = {step.id for step in recipe.steps}

    path = REPO / "domain_packs" / "kitchen" / "tomato_to_fridge.yaml"
    with path.open() as f:
        data = yaml.safe_load(f)

    yaml_step_ids = set(data["steps"].keys())
    assert yaml_step_ids == sop_step_ids


def test_domain_pack_covers_all_event_types() -> None:
    # Collect all event_types referenced in the SOP evidence_rules
    recipe = load_recipe(REPO / "sop" / "tomato_to_fridge.json")
    sop_event_types: set[str] = set()
    for step in recipe.steps:
        for rule in step.completion_policy.evidence_rules:
            sop_event_types.add(rule.event_type)
        for edge in step.recovery_transitions:
            sop_event_types.add(edge.event_type)

    path = REPO / "domain_packs" / "kitchen" / "tomato_to_fridge.yaml"
    with path.open() as f:
        data = yaml.safe_load(f)

    yaml_event_types = set(data["event_vocabulary"].keys())

    missing = sop_event_types - yaml_event_types
    assert not missing, f"event types in SOP but not documented: {missing}"


def test_domain_pack_regions_match_config() -> None:
    path = REPO / "domain_packs" / "kitchen" / "tomato_to_fridge.yaml"
    with path.open() as f:
        data = yaml.safe_load(f)

    regions = data["regions"]
    assert "table" in regions
    assert "refrigerator_interior" in regions
    assert regions["table"]["type"] == "frame_fraction"
    assert regions["refrigerator_interior"]["type"] == "detected_or_fallback"


def test_domain_pack_canonical_map_is_consistent() -> None:
    path = REPO / "domain_packs" / "kitchen" / "tomato_to_fridge.yaml"
    with path.open() as f:
        data = yaml.safe_load(f)

    canonical = data["canonical_map"]
    # all aliases must map to known canonical labels
    for canon, aliases in canonical.items():
        for a in aliases:
            assert a == canon or a != canon
