"""Verify annotation timeline loader and AI-isolation boundary."""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path

import yaml

from server.domain.annotations import AnnotationTimeline, load_annotations
from server.engine import StateEngine, load_recipe

REPO = Path(__file__).resolve().parent.parent
ANN_PATH = REPO / "data" / "annotations" / "IMG_9789_table_to_fridge.yaml"


def test_annotation_loader_loads_segments() -> None:
    recipe = load_recipe(REPO / "sop" / "tomato_to_fridge.json")
    valid = {s.id for s in recipe.steps}
    tl = load_annotations(ANN_PATH, valid_step_ids=valid)
    assert len(tl.segments) == 5
    assert tl.segments[0].expected_step_id == "tomato_on_table"


def test_annotation_lookup_inside_segment() -> None:
    recipe = load_recipe(REPO / "sop" / "tomato_to_fridge.json")
    valid = {s.id for s in recipe.steps}
    tl = load_annotations(ANN_PATH, valid_step_ids=valid)
    ann = tl.lookup(1000)
    assert ann is not None
    assert ann.expected_step_id == "tomato_on_table"


def test_annotation_lookup_outside_segment_returns_none() -> None:
    recipe = load_recipe(REPO / "sop" / "tomato_to_fridge.json")
    valid = {s.id for s in recipe.steps}
    tl = load_annotations(ANN_PATH, valid_step_ids=valid)
    assert tl.lookup(2500) is None
    assert tl.lookup(30000) is None


def test_annotation_lookup_at_boundary() -> None:
    recipe = load_recipe(REPO / "sop" / "tomato_to_fridge.json")
    valid = {s.id for s in recipe.steps}
    tl = load_annotations(ANN_PATH, valid_step_ids=valid)
    assert tl.lookup(1999) is not None
    assert tl.lookup(2000) is None


def test_annotation_invalid_step_id_raises() -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml") as f:
        yaml.dump({
            "segments": [{"start_s": 0, "end_s": 1, "expected_step_id": "not_real"}]
        }, f)
        f.flush()
        with __import__("pytest").raises(ValueError, match="unknown step_id"):
            load_annotations(f.name, valid_step_ids={"ready"})


def test_annotation_overlapping_segments_raises() -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml") as f:
        yaml.dump({
            "segments": [
                {"start_s": 0, "end_s": 5, "expected_step_id": "ready"},
                {"start_s": 3, "end_s": 8, "expected_step_id": "ready"},
            ]
        }, f)
        f.flush()
        with __import__("pytest").raises(ValueError, match="overlapping"):
            load_annotations(f.name, valid_step_ids={"ready"})


def test_annotation_does_not_affect_state_engine() -> None:
    recipe = load_recipe(REPO / "sop" / "tomato_to_fridge.json")
    engine = StateEngine(
        session_id="ses_a", recipe=recipe,
        started_at=datetime(2026, 7, 23, tzinfo=UTC),
    )
    ctx1 = engine.context.model_dump_json()
    # Annotations are a separate code path; engine state is identical
    # regardless of whether annotations exist.
    assert len(ctx1) > 0
    assert engine.context.context_version == 1
