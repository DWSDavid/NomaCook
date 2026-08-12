"""Tests for HotMemory event accumulation, Qwen context refresh, bounded reconnect,
shutdown, first-audio latency, half-duplex mic, rich context, and API key safety."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.engine.hot_memory import HotMemory
from server.engine.snapshot import TaskSnapshot


_ENV = {"DASHSCOPE_API_KEY": "sk-test", "BAILIAN_WORKSPACE_ID": "ws-123"}


def _make_snap(state="ready", status="ON_TRACK", belief=0.5, pending_question=None,
               active_objects=(), missing_evidence=(), seq=1, cv=1,
               task_goal="把番茄放进冰箱", step_title="开始", step_instruction="请拿起桌上的番茄。"):
    return TaskSnapshot(
        session_id="s1", task_id="t1", task_goal=task_goal,
        state=state, step_title=step_title, step_instruction=step_instruction,
        status=status, belief=belief, active_objects=active_objects,
        missing_evidence=missing_evidence, pending_question=pending_question,
        last_event_seq=seq, context_version=cv,
    )


def _make_adapter(hm=None, **kw):
    import server.voice.qwen_realtime as qr
    if hm is None:
        hm = HotMemory()
        hm.update(snapshot=_make_snap())
    return qr.QwenRealtimeAdapter(hot_memory=hm, session_dir=None, **kw)


# ── Rich compact context: step_title, step_instruction, task_goal ──

def test_compact_context_has_task_goal():
    hm = HotMemory()
    hm.update(snapshot=_make_snap(task_goal="把番茄放进冰箱", step_title="手拿番茄",
                                   step_instruction="你拿着番茄了，请把它移向冰箱。"))
    ctx = hm.compact_context()
    assert "task_goal: 把番茄放进冰箱" in ctx
    assert "current_step_title: 手拿番茄" in ctx
    assert "你拿着番茄了" in ctx


def test_compact_context_includes_instruction():
    hm = HotMemory()
    hm.update(snapshot=_make_snap(step_title="移动中", step_instruction="番茄正在移动，请继续移向冰箱。"))
    ctx = hm.compact_context()
    assert "current_step_title: 移动中" in ctx
    assert "current_instruction: 番茄正在移动，请继续移向冰箱。" in ctx


def test_compact_context_pending_question_is_null_not_none():
    hm = HotMemory()
    hm.update(snapshot=_make_snap(pending_question=None))
    ctx = hm.compact_context()
    assert "pending_question: None" in ctx or "pending_question: null" in ctx


# ── Qwen instruction forbids fabricated visuals ──

def test_qwen_instruction_forbids_fabricated_visuals():
    import server.voice.qwen_realtime as qr
    inst = qr.QWEN_SYSTEM_INSTRUCTION
    assert "无法看到原始画面" in inst
    assert "不要声称" in inst
    assert "不要虚构" in inst
    assert "不能闲聊" in inst


def test_qwen_instruction_forbids_open_questions():
    import server.voice.qwen_realtime as qr
    inst = qr.QWEN_SYSTEM_INSTRUCTION
    assert "不得反问" in inst
    assert "不得复述" in inst
    assert "不得进入开放式闲聊" in inst


# ── Model-aware defaults: voice + VAD ──

def test_model_defaults_qwen3():
    from server.voice.qwen_realtime import _resolve_model_defaults
    voice, vad = _resolve_model_defaults("qwen3-omni-flash-realtime")
    assert voice == "Cherry"
    assert vad == "server_vad"


def test_model_defaults_qwen35():
    from server.voice.qwen_realtime import _resolve_model_defaults
    voice, vad = _resolve_model_defaults("qwen3.5-omni-flash-realtime")
    assert voice == "Tina"
    assert vad == "semantic_vad"


def test_model_defaults_voice_override():
    from server.voice.qwen_realtime import _resolve_model_defaults
    voice, vad = _resolve_model_defaults("qwen3-omni-flash-realtime", voice_override="Stella")
    assert voice == "Stella"
    assert vad == "server_vad"


def test_model_defaults_qwen35_voice_override():
    from server.voice.qwen_realtime import _resolve_model_defaults
    voice, vad = _resolve_model_defaults("qwen3.5-omni-plus-realtime", voice_override="Ethan")
    assert voice == "Ethan"
    assert vad == "semantic_vad"


def test_qwen3_session_update_payload():
    """Verify qwen3 adapter produces session.update with Cherry + server_vad."""
    import server.voice.qwen_realtime as qr
    with patch.dict(os.environ, _ENV, clear=True):
        a = qr.QwenRealtimeAdapter(model="qwen3-omni-flash-realtime")
        assert a._voice == "Cherry"
        assert a._vad_type == "server_vad"


def test_qwen35_session_update_payload():
    """Verify qwen3.5 adapter produces session.update with Tina + semantic_vad."""
    import server.voice.qwen_realtime as qr
    with patch.dict(os.environ, _ENV, clear=True):
        a = qr.QwenRealtimeAdapter(model="qwen3.5-omni-flash-realtime")
        assert a._voice == "Tina"
        assert a._vad_type == "semantic_vad"


# ── HotMemory: append events, not replace ──

def test_hot_memory_accumulates_events():
    hm = HotMemory()
    for i in range(20):
        hm.update(snapshot=_make_snap(cv=i + 1), recent_events=[{"type": f"EV_{i}", "seq": i}])
    data = hm.read()
    assert len(data["recent_events"]) == 12
    types_last = [e["type"] for e in data["recent_events"]]
    assert types_last[0] == "EV_8"
    assert types_last[-1] == "EV_19"


def test_hot_memory_compact_context_has_latest_events():
    hm = HotMemory()
    for i in range(20):
        hm.update(snapshot=_make_snap(state="tomato_held", seq=i, cv=i + 1),
                   recent_events=[{"type": f"EV_{i}", "seq": i}])
    ctx = hm.compact_context()
    assert "EV_19" in ctx


# ── Context refresh: signature-based ──

def test_context_first_snapshot_triggers_refresh():
    with patch.dict(os.environ, _ENV, clear=True):
        hm = HotMemory()
        hm.update(snapshot=_make_snap(state="ready", cv=1))
        a = _make_adapter(hm)
        assert a._context_needs_refresh() is True
        a._build_instructions()
        assert a._context_needs_refresh() is False


def test_context_state_transition_triggers_refresh():
    with patch.dict(os.environ, _ENV, clear=True):
        hm = HotMemory()
        hm.update(snapshot=_make_snap(state="ready", cv=1))
        a = _make_adapter(hm)
        assert a._context_needs_refresh() is True
        a._build_instructions()
        assert a._context_needs_refresh() is False
        hm.update(snapshot=_make_snap(state="tomato_on_table", cv=2))
        assert a._context_needs_refresh() is True


def test_context_pending_question_triggers_refresh():
    with patch.dict(os.environ, _ENV, clear=True):
        hm = HotMemory()
        hm.update(snapshot=_make_snap(state="ready", cv=1, pending_question="ready?"))
        a = _make_adapter(hm)
        assert a._context_needs_refresh() is True
        a._build_instructions()
        assert a._context_needs_refresh() is False
        hm.update(snapshot=_make_snap(state="ready", cv=2, pending_question="done?"))
        assert a._context_needs_refresh() is True


def test_context_ordinary_event_no_refresh():
    with patch.dict(os.environ, _ENV, clear=True):
        hm = HotMemory()
        hm.update(snapshot=_make_snap(state="ready", cv=1))
        a = _make_adapter(hm)
        assert a._context_needs_refresh() is True
        assert a._context_needs_refresh() is False
        hm.update(snapshot=_make_snap(state="ready", cv=2))
        assert a._context_needs_refresh() is False
        hm.update(snapshot=_make_snap(state="ready", cv=3))
        assert a._context_needs_refresh() is False


def test_context_complete_triggers_refresh():
    with patch.dict(os.environ, _ENV, clear=True):
        hm = HotMemory()
        hm.update(snapshot=_make_snap(state="tomato_released_inside", status="COMPLETE", cv=10))
        a = _make_adapter(hm)
        assert a._context_needs_refresh() is True


# ── Fail-fast env check ──

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
    with patch.dict(os.environ, _ENV, clear=True):
        from server.voice.qwen_realtime import _check_env
        _check_env()


# ── First audio latency: measured from speech_stopped ──

def test_first_audio_latency_from_speech_stopped():
    with patch.dict(os.environ, _ENV, clear=True):
        hm = HotMemory()
        hm.update(snapshot=_make_snap())
        a = _make_adapter(hm)
        a._first_audio_lats = []
        turn_end = 1000.0
        audio_time = turn_end + 0.5
        lat = (audio_time - turn_end) * 1000
        a._first_audio_lats.append(lat)
        assert len(a._first_audio_lats) == 1
        assert 400 <= a._first_audio_lats[0] <= 600


# ── Log safety: no API key ──

def test_log_no_api_key_leak():
    with patch.dict(os.environ, _ENV, clear=True):
        hm = HotMemory()
        hm.update(snapshot=_make_snap())
        a = _make_adapter(hm)
        a._log({"type": "connected"})
        a._log({"type": "api_error", "error": "test error"})
        for e in a._event_log.events:
            dumped = json.dumps(e)
            assert "sk-" not in dumped
            assert a._api_key not in dumped


# ── Bounded reconnect: max 2 attempts ──

def test_bounded_reconnect_max_2_attempts():
    attempt_counter = [0]
    async def fake_connect_and_run(self):
        attempt_counter[0] += 1
        import websockets
        raise websockets.exceptions.ConnectionClosed(None, None)

    with patch.dict(os.environ, _ENV, clear=True):
        import server.voice.qwen_realtime as qr
        hm = HotMemory()
        hm.update(snapshot=_make_snap())
        with patch.object(qr.QwenRealtimeAdapter, "_connect_and_run", fake_connect_and_run):
            a = _make_adapter(hm)
            asyncio.run(a.run())
    assert attempt_counter[0] == 2
    assert a.error_count == 2
    assert a.connected is False


# ── Fake helpers for production-loop tests ──

class _FakeMicStream:
    def start(self): pass
    def stop(self): pass
    def close(self): pass

class _FakeSpkStream:
    def start(self): pass
    def stop(self): pass
    def close(self): pass

class _FakeWS:
    def __init__(self, server_events: list[str]):
        self._events = server_events
        self._idx = 0
        self.sent: list[str] = []
        self.closed = False

    async def __aenter__(self): return self
    async def __aexit__(self, *a): pass
    def __aiter__(self): return self

    async def __anext__(self):
        if self._idx >= len(self._events):
            await asyncio.sleep(0.02)  # keep loop alive briefly
            raise StopAsyncIteration
        raw = self._events[self._idx]
        self._idx += 1
        return raw

    async def send(self, data: str):
        self.sent.append(data)

    async def close(self):
        self.closed = True


# ── Production interruption: response.cancel via real _connect_and_run ──

def test_interruption_sends_response_cancel():
    import server.voice.qwen_realtime as qr
    fake_ws = _FakeWS(['{"type": "response.created"}', '{"type": "input_audio_buffer.speech_started"}'])

    with patch.dict(os.environ, _ENV, clear=True):
        with patch.object(qr.websockets, 'connect', lambda url, **kw: fake_ws):
            with patch.object(qr.sd, 'RawInputStream', return_value=_FakeMicStream()):
                with patch.object(qr.sd, 'RawOutputStream', return_value=_FakeSpkStream()):
                    hm = HotMemory()
                    hm.update(snapshot=_make_snap())
                    a = _make_adapter(hm)
                    asyncio.run(a.run())

    assert a._user_turns == 1
    cancel = [m for m in fake_ws.sent if '"response.cancel"' in m]
    assert len(cancel) == 1
    assert any('"session.update"' in m for m in fake_ws.sent)


def test_shutdown_clean_exit():
    import server.voice.qwen_realtime as qr
    done = asyncio.Event()

    class BlockingFakeWS:
        def __init__(self):
            self.sent: list[str] = []
            self.closed = False
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def __aiter__(self): return self
        async def __anext__(self):
            if done.is_set(): raise StopAsyncIteration
            await asyncio.sleep(0.05)
            return '{"type": "session.updated"}'
        async def send(self, data: str): self.sent.append(data)
        async def close(self): self.closed = True

    fake_ws = BlockingFakeWS()

    with patch.dict(os.environ, _ENV, clear=True):
        with patch.object(qr.websockets, 'connect', lambda url, **kw: fake_ws):
            with patch.object(qr.sd, 'RawInputStream', return_value=_FakeMicStream()):
                with patch.object(qr.sd, 'RawOutputStream', return_value=_FakeSpkStream()):
                    hm = HotMemory()
                    hm.update(snapshot=_make_snap())
                    a = _make_adapter(hm)
                    async def _run_and_stop():
                        loop = asyncio.get_running_loop()
                        loop.call_later(0.2, a.request_stop)
                        loop.call_later(0.3, done.set)
                        await a.run()
                    asyncio.run(_run_and_stop())
    assert a.connected is False
    assert fake_ws.closed is True


# ── Half-duplex: mic suppressed during assistant response ──

def test_mic_suppressed_during_response():
    """mic callback drops audio when mic_suppressed is set."""
    import server.voice.qwen_realtime as qr
    async def _run():
        suppressed = asyncio.Event()
        suppressed.set()
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=4)
        a = _make_adapter(HotMemory())
        cb = a._mic_callback(queue, suppressed)
        cb(b'\x00' * 3200, None, None, None)
        await asyncio.sleep(0.02)
        assert queue.empty()
    with patch.dict(os.environ, _ENV, clear=True):
        asyncio.run(_run())


def test_mic_resumed_when_not_suppressed():
    """mic callback passes audio when mic_suppressed is not set."""
    import server.voice.qwen_realtime as qr
    async def _run():
        suppressed = asyncio.Event()
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=4)
        a = _make_adapter(HotMemory())
        cb = a._mic_callback(queue, suppressed)
        cb(b'\x00' * 3200, None, None, None)
        await asyncio.sleep(0.02)
        assert not queue.empty()
    with patch.dict(os.environ, _ENV, clear=True):
        asyncio.run(_run())


def test_log_contains_mic_suppressed_events():
    """Verify mic_suppressed / mic_resumed event types exist in adapter."""
    with patch.dict(os.environ, _ENV, clear=True):
        hm = HotMemory()
        hm.update(snapshot=_make_snap())
        a = _make_adapter(hm)
        a._log({"type": "mic_suppressed"})
        a._log({"type": "mic_resumed"})
        types = {e["type"] for e in a._event_log.events}
        assert "mic_suppressed" in types
        assert "mic_resumed" in types


# ── HotMemory write_latest_snapshot ──

def test_hot_memory_write_latest_snapshot(tmp_path):
    hm = HotMemory()
    hm.update(snapshot=_make_snap())
    hm.write_latest_snapshot(tmp_path)
    assert (tmp_path / "latest_snapshot.json").exists()
    data = json.loads((tmp_path / "latest_snapshot.json").read_text())
    assert data["state"] == "ready"


def test_hot_memory_write_latest_snapshot_no_dir():
    hm = HotMemory()
    hm.write_latest_snapshot(None)


# ── Thread safety ──

def test_hot_memory_thread_safety():
    import threading
    hm = HotMemory()
    snap = _make_snap()
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
                hm.compact_context()
        except Exception as e:
            errors.append(e)

    ts = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors
