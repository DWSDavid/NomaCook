from __future__ import annotations

import json
from pathlib import Path

from server.engine.sop import load_recipe


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_tomato_to_fridge_contract_has_expected_graph() -> None:
    recipe = load_recipe(REPO_ROOT / "sop" / "tomato_to_fridge.json")

    assert recipe.recipe_version_id == "tomato_to_fridge_v1"
    assert [step.id for step in recipe.steps] == [
        "ready",
        "tomato_on_table",
        "hand_near_tomato",
        "tomato_held",
        "tomato_in_transit",
        "fridge_interaction",
        "candidate_inside_fridge",
        "tomato_released_inside",
    ]
    held = next(step for step in recipe.steps if step.id == "tomato_held")
    assert held.next_step_id == "tomato_in_transit"
    assert {
        (edge.event_type, edge.target_step_id)
        for edge in held.recovery_transitions
    } == {("OBJECT_RETURNED_TO_REGION", "tomato_on_table")}


def test_tomato_to_fridge_completion_policy_fields() -> None:
    recipe = load_recipe(REPO_ROOT / "sop" / "tomato_to_fridge.json")
    transit = next(step for step in recipe.steps if step.id == "tomato_in_transit")
    policy = transit.completion_policy
    assert policy.min_source_groups == 2
    assert policy.evidence_window_ms == 3000
    assert policy.threshold == 0.8
    assert policy.consecutive_hits == 2


def test_tomato_to_fridge_source_groups_match_policy() -> None:
    recipe = load_recipe(REPO_ROOT / "sop" / "tomato_to_fridge.json")
    held = next(step for step in recipe.steps if step.id == "tomato_held")
    assert held.completion_policy.min_source_groups == 2
    groups = {rule.source_group for rule in held.completion_policy.evidence_rules}
    assert groups == {"hand_relation", "motion"}
    assert len(groups) >= held.completion_policy.min_source_groups


def test_final_step_has_no_next_step() -> None:
    recipe = load_recipe(REPO_ROOT / "sop" / "tomato_to_fridge.json")
    final = recipe.steps[-1]
    assert final.id == "tomato_released_inside"
    assert final.next_step_id is None


def test_final_step_can_recover_to_held() -> None:
    recipe = load_recipe(REPO_ROOT / "sop" / "tomato_to_fridge.json")
    final = recipe.steps[-1]
    recovery_ids = {edge.target_step_id for edge in final.recovery_transitions}
    assert "tomato_held" in recovery_ids


def test_old_sops_still_load_without_new_fields() -> None:
    recipe = load_recipe(REPO_ROOT / "sop" / "fried_rice.json")
    assert recipe.dish == "蛋炒饭"
    for step in recipe.steps:
        assert step.next_step_id is None
        assert step.recovery_transitions == ()
        for rule in step.completion_policy.evidence_rules:
            assert rule.source_group == "default"
        assert step.completion_policy.min_source_groups == 1
        assert step.completion_policy.evidence_window_ms == 5000


# ── Fix 5: frozen schema declares all task-contract fields ──


def test_schema_declares_task_graph_fields() -> None:
    schema_text = (REPO_ROOT / "sop" / "schema.json").read_text(encoding="utf-8")
    schema = json.loads(schema_text)

    step_props = schema["$defs"]["step"]["properties"]
    assert "next_step_id" in step_props
    assert "recovery_transitions" in step_props

    policy_props = schema["$defs"]["completionPolicy"]["properties"]
    assert "evidence_window_ms" in policy_props
    assert "min_source_groups" in policy_props

    rule_props = schema["$defs"]["evidenceRule"]["properties"]
    assert "source_group" in rule_props

    recovery = schema["$defs"]["recoveryTransition"]
    assert recovery["required"] == ["event_type", "payload_matches", "target_step_id"]
    assert "event_type" in recovery["properties"]
    assert "payload_matches" in recovery["properties"]
    assert "target_step_id" in recovery["properties"]


def test_tomato_to_fridge_json_passes_pydantic_validation() -> None:
    recipe = load_recipe(REPO_ROOT / "sop" / "tomato_to_fridge.json")
    for step in recipe.steps:
        if step.next_step_id is not None:
            assert step.next_step_id != ""
        for edge in step.recovery_transitions:
            assert edge.event_type != ""
            assert edge.target_step_id != ""
            assert isinstance(edge.payload_matches, dict)
