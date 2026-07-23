from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from server.pipeline.session import (
    SESSION_EPOCH,
    SessionPaths,
    create_run_dir,
    event_id_for,
    session_id_for,
    t_server_for,
)


def test_ids_and_clock_are_deterministic_and_filename_safe():
    sid = session_id_for("data/test_videos/My Video (1).mp4", "rv_tomato_egg_demo_1")
    assert sid == session_id_for("data/test_videos/My Video (1).mp4", "rv_tomato_egg_demo_1")
    assert sid == "ses_rv_tomato_egg_demo_1_my_video_1"
    assert event_id_for(sid, 7) == f"evt_{sid}_00000007"
    assert SESSION_EPOCH == datetime(2026, 1, 1, tzinfo=UTC)
    assert t_server_for(1500.0) == datetime(2026, 1, 1, 0, 0, 1, 500000, tzinfo=UTC)


def test_run_dir_layout(tmp_path: Path):
    paths = create_run_dir("ses_x", base=tmp_path, run_tag="a")
    assert paths.root == tmp_path / "ses_x" / "run_a"
    assert paths.keyframes_dir.is_dir()
    assert paths.events == paths.root / "events.jsonl"
    assert paths.timeline == paths.root / "timeline.jsonl"
    assert paths.annotated == paths.root / "annotated.mp4"
    assert paths.report == paths.root / "report.md"
    assert paths.meta == paths.root / "meta.json"


def test_same_run_tag_twice_raises(tmp_path: Path):
    create_run_dir("ses_x", base=tmp_path, run_tag="a")
    try:
        create_run_dir("ses_x", base=tmp_path, run_tag="a")
        raise AssertionError("expected FileExistsError")
    except FileExistsError:
        pass
