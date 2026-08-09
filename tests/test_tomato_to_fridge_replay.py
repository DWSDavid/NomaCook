from __future__ import annotations

from pathlib import Path

from server.engine import StateEngine, load_recipe
from server.engine.snapshot import TaskSnapshot, build_task_snapshot
from server.events.log import read_events


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "tomato_to_fridge"
SOP_PATH = Path(__file__).resolve().parent.parent / "sop" / "tomato_to_fridge.json"


def replay_fixture(name: str) -> list[TaskSnapshot]:
    recipe = load_recipe(SOP_PATH)
    events = read_events(FIXTURE_DIR / name)
    if not events:
        return []
    engine = StateEngine(
        session_id=events[0].session_id,
        recipe=recipe,
        started_at=events[0].t_server_est,
    )
    snapshots: list[TaskSnapshot] = []
    for event in events:
        engine.consume(event)
        snapshots.append(build_task_snapshot(engine.context, engine.current_step))
    return snapshots


def test_fixture_files_exist() -> None:
    assert (FIXTURE_DIR / "happy_path.jsonl").exists()
    assert (FIXTURE_DIR / "put_back_on_table.jsonl").exists()
    assert (FIXTURE_DIR / "occluded_release.jsonl").exists()


def test_happy_path_reaches_complete() -> None:
    snapshots = replay_fixture("happy_path.jsonl")
    assert snapshots[-1].status == "COMPLETE"


def test_put_back_returns_to_table_without_completion() -> None:
    snapshots = replay_fixture("put_back_on_table.jsonl")
    assert snapshots[-1].state == "tomato_on_table"
    assert snapshots[-1].status != "COMPLETE"


def test_occluded_release_remains_uncertain() -> None:
    snapshots = replay_fixture("occluded_release.jsonl")
    assert snapshots[-1].state == "tomato_released_inside"
    assert snapshots[-1].status == "UNCERTAIN"
    assert "stable_inside" in snapshots[-1].missing_evidence


def test_replay_is_deterministic() -> None:
    first = [item.model_dump_json() for item in replay_fixture("happy_path.jsonl")]
    second = [item.model_dump_json() for item in replay_fixture("happy_path.jsonl")]
    assert first == second
