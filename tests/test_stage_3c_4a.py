"""Minimal tests for HotMemory, Qwen adapter env-check, and integration invariants."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.engine.hot_memory import HotMemory
from server.engine.snapshot import TaskSnapshot


def test_hot_memory_update_and_read():
    hm = HotMemory()
    snap = TaskSnapshot(
        session_id="s1", task_id="t1", state="ready",
        status="ON_TRACK", belief=0.5, active_objects=("tomato",),
        missing_evidence=("hand_near",), pending_question=None,
        last_event_seq=3, context_version=1,
    )
    hm.update(snapshot=snap, recent_events=[{"type": "OBJECT_PRESENT", "seq": 3}])
    data = hm.read()
    assert data["snapshot"]["state"] == "ready"
    assert data["context_version"] == 1
    assert len(data["recent_events"]) == 1


def test_hot_memory_compact_context():
    hm = HotMemory()
    snap = TaskSnapshot(
        session_id="s1", task_id="t1", state="tomato_held",
        status="ON_TRACK", belief=0.84, active_objects=("tomato",),
        missing_evidence=("OBJECT_MOVING_WITH_HAND",), pending_question=None,
        last_event_seq=5, context_version=3,
    )
    hm.update(snapshot=snap, recent_events=[
        {"type": "HOLDING_STARTED", "seq": 5},
    ])
    ctx = hm.compact_context()
    assert "tomato_held" in ctx
    assert "ON_TRACK" in ctx
    assert "0.84" in ctx
    assert "HOLDING_STARTED" in ctx


def test_hot_memory_bounded_recent_events():
    hm = HotMemory()
    import server.engine.hot_memory as hm_mod
    events = [{"type": f"EV_{i}", "seq": i} for i in range(20)]
    hm.update(recent_events=events)
    data = hm.read()
    assert len(data["recent_events"]) <= hm_mod.MAX_RECENT_EVENTS


def test_hot_memory_thread_safety():
    import threading
    hm = HotMemory()
    snap = TaskSnapshot(
        session_id="s1", task_id="t1", state="ready",
        status="ON_TRACK", belief=0.5, active_objects=(), missing_evidence=(),
        pending_question=None, last_event_seq=1, context_version=1,
    )

    errors = []

    def writer():
        try:
            for i in range(100):
                hm.update(snapshot=snap, recent_events=[{"type": "T", "seq": i}])
        except Exception as e:
            errors.append(e)

    def reader():
        try:
            for _ in range(100):
                hm.read()
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors


def test_qwen_fail_fast_missing_env():
    with patch.dict(os.environ, {}, clear=True):
        from server.voice.qwen_realtime import _check_env
        with pytest.raises(RuntimeError, match="DASHSCOPE_API_KEY"):
            _check_env()

    with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "sk-test"}, clear=True):
        from server.voice.qwen_realtime import _check_env
        with pytest.raises(RuntimeError, match="BAILIAN_WORKSPACE_ID"):
            _check_env()


def test_qwen_fail_fast_env_ok():
    with patch.dict(os.environ, {
        "DASHSCOPE_API_KEY": "sk-test",
        "BAILIAN_WORKSPACE_ID": "ws-123",
    }, clear=True):
        from server.voice.qwen_realtime import _check_env
        _check_env()  # should not raise


def test_qwen_not_started_without_flag():
    """Verify that qwen_realtime module imports but env check fails without keys."""
    with patch.dict(os.environ, {}, clear=True):
        from server.voice.qwen_realtime import _check_env
        with pytest.raises(RuntimeError):
            _check_env()


def test_hot_memory_write_latest_snapshot(tmp_path):
    hm = HotMemory()
    snap = TaskSnapshot(
        session_id="s1", task_id="t1", state="ready",
        status="ON_TRACK", belief=0.5, active_objects=(), missing_evidence=(),
        pending_question=None, last_event_seq=1, context_version=1,
    )
    hm.update(snapshot=snap)
    hm.write_latest_snapshot(tmp_path)
    assert (tmp_path / "latest_snapshot.json").exists()
    import json
    data = json.loads((tmp_path / "latest_snapshot.json").read_text())
    assert data["state"] == "ready"


def test_hot_memory_write_latest_snapshot_no_dir():
    """Should not crash when session_dir is None."""
    hm = HotMemory()
    hm.write_latest_snapshot(None)  # no-op
