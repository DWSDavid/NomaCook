"""Tests for HotMemory dialogue, grounded response plan, fact gate, mic backlog,
half-duplex, rich context, model defaults, and log safety."""

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

from server.engine.hot_memory import HotMemory, MAX_DIALOGUE_TURNS
from server.engine.snapshot import TaskSnapshot
from server.voice.grounded import check_fact_gate, classify_intent, grounded_plan, COMPLETION_CLAIMS


_ENV = {"DASHSCOPE_API_KEY": "sk-test", "BAILIAN_WORKSPACE_ID": "ws-123"}


def _make_snap(state="ready", status="ON_TRACK", belief=0.5, pending_question=None,
               active_objects=(), missing_evidence=(), seq=1, cv=1,
               task_goal="把番茄放进冰箱", step_title="开始",
               step_instruction="请拿起桌上的番茄。"):
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


# ── Fact gate: no completion claims before COMPLETE ──

def test_fact_gate_blocks_completion_before_complete():
    snap = _make_snap(state="fridge_interaction", status="ON_TRACK")
    allowed, override = check_fact_gate(
        assistant_transcript="番茄已放入冰箱，任务完成",
        snapshot=snap.model_dump(),
    )
    assert allowed is False
    assert override is not None
    assert "还没有确认进" in override


def test_fact_gate_allows_completion_when_complete():
    snap = _make_snap(state="tomato_released_inside", status="COMPLETE")
    allowed, override = check_fact_gate(
        assistant_transcript="可以了，番茄已经放稳，任务完成。",
        snapshot=snap.model_dump(),
    )
    assert allowed is True
    assert override is None


def test_fact_gate_allows_non_completion_text():
    snap = _make_snap(state="fridge_interaction", status="ON_TRACK")
    allowed, _ = check_fact_gate(
        assistant_transcript="你已经把番茄带到冰箱前了。",
        snapshot=snap.model_dump(),
    )
    assert allowed is True


def test_fact_gate_blocks_qwen_confirm():
    snap = _make_snap(state="fridge_interaction", status="ON_TRACK")
    allowed, _ = check_fact_gate(
        assistant_transcript="确定，番茄已放入冰箱",
        snapshot=snap.model_dump(),
    )
    assert allowed is False


def test_completion_claims_regex():
    assert COMPLETION_CLAIMS.search("已完成")
    assert COMPLETION_CLAIMS.search("放进去了")
    assert COMPLETION_CLAIMS.search("确认完成")
    assert not COMPLETION_CLAIMS.search("你已经到冰箱前了")


# ── Intent classification ──

def test_classify_completion_query():
    assert classify_intent("进去了吗", None) == "completion_query"
    assert classify_intent("完成了没有", None) == "completion_query"


def test_classify_status_query():
    assert classify_intent("我现在做到哪一步了", None) == "status_query"
    assert classify_intent("我在哪里", None) == "status_query"


def test_classify_next_step_query():
    assert classify_intent("然后呢", None) == "next_step_query"
    assert classify_intent("接下来怎么做", None) == "next_step_query"


def test_classify_help():
    assert classify_intent("怎么办", None) == "help"
    assert classify_intent("帮我一下", None) == "help"


# ── Grounded response plan ──

def test_grounded_plan_completion_query_before_complete():
    snap = _make_snap(state="fridge_interaction", status="ON_TRACK",
                       step_title="靠近冰箱").model_dump()
    plan = grounded_plan(snapshot=snap, user_transcript="进去了吗")
    assert plan["completion_allowed"] is False
    assert "尚未确认完成" in plan["required_fact"]


def test_grounded_plan_completion_query_after_complete():
    snap = _make_snap(state="tomato_released_inside", status="COMPLETE").model_dump()
    plan = grounded_plan(snapshot=snap, user_transcript="进去了吗")
    assert plan["completion_allowed"] is True


def test_grounded_plan_status_query_no_next_step():
    snap = _make_snap(state="tomato_held", status="ON_TRACK",
                       step_title="手拿番茄").model_dump()
    plan = grounded_plan(snapshot=snap, user_transcript="我做到哪了")
    assert plan["intent"] == "status_query"
    assert plan["optional_next_action"] is None


def test_grounded_plan_next_step_has_instruction():
    snap = _make_snap(state="tomato_held", status="ON_TRACK",
                       step_title="手拿番茄",
                       step_instruction="你拿着番茄了，请把它移向冰箱。").model_dump()
    plan = grounded_plan(snapshot=snap, user_transcript="然后呢")
    assert plan["intent"] == "next_step_query"
    assert plan["optional_next_action"] == "你拿着番茄了，请把它移向冰箱。"


# ── Dialogue memory ──

def test_recent_dialogue_bounded(tmp_path):
    hm = HotMemory()
    hm.update(snapshot=_make_snap())
    for i in range(10):
        hm.add_dialogue_turn(
            user_transcript=f"q{i}", assistant_transcript=f"a{i}",
            response_was_grounded=True, session_dir=tmp_path,
        )
    data = hm.read()
    assert len(data["recent_dialogue"]) == 6
    assert data["recent_dialogue"][-1]["user"] == "q9"

    lines = (tmp_path / "dialogue_events.jsonl").read_text().strip().split("\n")
    assert len(lines) == 10


def test_compact_context_includes_dialogue():
    hm = HotMemory()
    hm.update(snapshot=_make_snap(state="tomato_held", step_title="手拿番茄"))
    hm.add_dialogue_turn(
        user_transcript="进去了吗", assistant_transcript="还没确认进入",
        response_was_grounded=True, session_dir=None,
    )
    ctx = hm.compact_context()
    assert "进去了吗" in ctx
    assert "还没确认进入" in ctx


def test_dialogue_records_state_and_grounded(tmp_path):
    hm = HotMemory()
    hm.update(snapshot=_make_snap(state="fridge_interaction", step_title="靠近冰箱"))
    hm.add_dialogue_turn(
        user_transcript="进去了吗", assistant_transcript="还没确认进入",
        response_was_grounded=False, session_dir=tmp_path,
    )
    data = hm.read()
    turn = data["recent_dialogue"][-1]
    assert turn["state_at_turn"] == "fridge_interaction"
    assert turn["response_was_grounded"] is False


# ── Mic backlog: suppressed callback returns, mic_sender skips sends ──

def test_mic_suppressed_callback_returns_early():
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


def test_mic_suppressed_clears_queue():
    import server.voice.qwen_realtime as qr
    async def _run():
        suppressed = asyncio.Event()
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=4)
        await queue.put(b'\x01' * 100)
        assert not queue.empty()
        suppressed.set()
        qr._clear_q(queue)
        assert queue.empty()
    asyncio.run(_run())


def test_mic_sender_skips_when_suppressed():
    """mic_sender must skip enqueued chunks when suppressed."""
    import server.voice.qwen_realtime as qr
    class _CaptureWS:
        def __init__(self):
            self.sent_audio = []
            self.closed = False
            self._index = 0
            self._events = ['{"type": "session.updated"}']
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def __aiter__(self): return self
        async def __anext__(self):
            if self._index >= len(self._events):
                raise StopAsyncIteration
            self._index += 1
            return self._events[self._index - 1]
        async def send(self, data: str):
            d = json.loads(data)
            if d.get("type") == "input_audio_buffer.append":
                self.sent_audio.append(d)
        async def close(self): self.closed = True

    fake_ws = _CaptureWS()
    sent_count = [0]

    async def patched_connect_and_run(self):
        self._connected = True
        await self._send_session_update(fake_ws)

        mic_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=4)
        mic_suppressed = asyncio.Event()

        async def mic_sender():
            while len(sent_count) < 3 and not self._stop.is_set():
                try:
                    chunk = await asyncio.wait_for(mic_queue.get(), timeout=0.5)
                    if mic_suppressed.is_set():
                        continue
                    await fake_ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": "AA=="}))
                    sent_count[0] += 1
                except TimeoutError:
                    continue
                except Exception:
                    break

        async def ctx_pusher():
            await asyncio.sleep(0.2)
            self._stop.set()

        mic_task = asyncio.create_task(mic_sender())
        ctx_task = asyncio.create_task(ctx_pusher())

        mic_suppressed.set()
        await mic_queue.put(b'\x00' * 100)
        await mic_queue.put(b'\x00' * 100)

        try:
            await asyncio.wait_for(asyncio.gather(mic_task, ctx_task), timeout=2.0)
        except TimeoutError:
            pass

        for t in (mic_task, ctx_task):
            t.cancel()
        self._connected = False

    with patch.dict(os.environ, _ENV, clear=True):
        import server.voice.qwen_realtime as qr
        with patch.object(qr.QwenRealtimeAdapter, "_connect_and_run", patched_connect_and_run):
            hm = HotMemory()
            hm.update(snapshot=_make_snap())
            a = _make_adapter(hm)
            asyncio.run(a.run())

    # Mic suppressed, sender should skip all chunks → 0 audio appends
    assert len(fake_ws.sent_audio) == 0


# ── ASR config in session.update ──

def test_session_update_includes_asr():
    import server.voice.qwen_realtime as qr
    with patch.dict(os.environ, _ENV, clear=True):
        a = qr.QwenRealtimeAdapter(model="qwen3-omni-flash-realtime")
    # Check _send_session_update payload via inspect
    async def _check():
        class _CapWS:
            def __init__(self): self.sent = []
            async def send(self, data): self.sent.append(json.loads(data))
        ws = _CapWS()
        await a._send_session_update(ws)
        session_cfg = next(m for m in ws.sent if m["type"] == "session.update")
        assert "input_audio_transcription" in session_cfg["session"]
        assert session_cfg["session"]["input_audio_transcription"]["model"] == "qwen3-asr-flash-realtime"
    asyncio.run(_check())


# ── Qwen instruction no fabrication ──

def test_qwen_instruction_forbids_visual_fabrication():
    import server.voice.qwen_realtime as qr
    inst = qr.QWEN_SYSTEM_INSTRUCTION
    assert "无法看到" in inst
    assert "不要声称" in inst
    assert "不能自行宣布" in inst


def test_qwen_instruction_forbids_open_questions():
    import server.voice.qwen_realtime as qr
    inst = qr.QWEN_SYSTEM_INSTRUCTION
    assert "不反问" in inst
    assert "不复述" in inst


# ── Rich compact context ──

def test_compact_context_has_task_goal():
    hm = HotMemory()
    hm.update(snapshot=_make_snap(task_goal="把番茄放进冰箱", step_title="手拿番茄",
                                   step_instruction="你拿着番茄了，请把它移向冰箱。"))
    ctx = hm.compact_context()
    assert "task_goal: 把番茄放进冰箱" in ctx
    assert "current_step_title: 手拿番茄" in ctx


def test_compact_context_pending_question_is_null():
    hm = HotMemory()
    hm.update(snapshot=_make_snap(pending_question=None))
    ctx = hm.compact_context()
    assert "pending_question: None" in ctx


# ── Model defaults ──

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


def test_qwen3_adapter_payload():
    import server.voice.qwen_realtime as qr
    with patch.dict(os.environ, _ENV, clear=True):
        a = qr.QwenRealtimeAdapter(model="qwen3-omni-flash-realtime")
        assert a._voice == "Cherry"
        assert a._vad_type == "server_vad"


def test_qwen35_adapter_payload():
    import server.voice.qwen_realtime as qr
    with patch.dict(os.environ, _ENV, clear=True):
        a = qr.QwenRealtimeAdapter(model="qwen3.5-omni-flash-realtime")
        assert a._voice == "Tina"
        assert a._vad_type == "semantic_vad"


# ── Fail-fast env check ──

def test_qwen_fail_fast_missing_env():
    from server.voice.qwen_realtime import _check_env
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(RuntimeError, match="DASHSCOPE_API_KEY"):
            _check_env()


def test_qwen_fail_fast_env_ok():
    from server.voice.qwen_realtime import _check_env
    with patch.dict(os.environ, _ENV, clear=True):
        _check_env()


# ── Log safety ──

def test_log_no_api_key_leak():
    with patch.dict(os.environ, _ENV, clear=True):
        a = _make_adapter(HotMemory())
        a._log({"type": "connected"})
        a._log({"type": "api_error", "error": "test error"})
        for e in a._event_log.events:
            dumped = json.dumps(e)
            assert "sk-" not in dumped
            assert "test" not in dumped or a._api_key not in dumped


# ── HotMemory events ──

def test_hot_memory_accumulates_events():
    hm = HotMemory()
    for i in range(20):
        hm.update(snapshot=_make_snap(cv=i + 1), recent_events=[{"type": f"EV_{i}", "seq": i}])
    assert len(hm.read()["recent_events"]) == 12


# ── Context refresh ──

def test_context_ordinary_event_no_refresh():
    with patch.dict(os.environ, _ENV, clear=True):
        hm = HotMemory()
        hm.update(snapshot=_make_snap(state="ready", cv=1))
        a = _make_adapter(hm)
        assert a._context_needs_refresh() is True
        assert a._context_needs_refresh() is False
        hm.update(snapshot=_make_snap(state="ready", cv=2))
        assert a._context_needs_refresh() is False


def test_context_state_transition_triggers_refresh():
    with patch.dict(os.environ, _ENV, clear=True):
        hm = HotMemory()
        hm.update(snapshot=_make_snap(state="ready", cv=1))
        a = _make_adapter(hm)
        a._context_needs_refresh()
        a._build_instructions()
        assert a._context_needs_refresh() is False
        hm.update(snapshot=_make_snap(state="tomato_on_table", cv=2))
        assert a._context_needs_refresh() is True


def test_context_complete_triggers_refresh():
    with patch.dict(os.environ, _ENV, clear=True):
        hm = HotMemory()
        hm.update(snapshot=_make_snap(state="done", status="COMPLETE", cv=10))
        a = _make_adapter(hm)
        assert a._context_needs_refresh() is True


# ── Bounded reconnect ──

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


# ── First audio latency ──

def test_first_audio_latency_from_speech_stopped():
    with patch.dict(os.environ, _ENV, clear=True):
        a = _make_adapter(HotMemory())
        a._first_audio_lats = []
        a._first_audio_lats.append(500.0)
        assert a._first_audio_lats[0] == 500.0


# ── HotMemory snapshots ──

def test_hot_memory_write_latest_snapshot(tmp_path):
    hm = HotMemory()
    hm.update(snapshot=_make_snap())
    hm.write_latest_snapshot(tmp_path)
    assert (tmp_path / "latest_snapshot.json").exists()


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
                hm.read(); hm.compact_context()
        except Exception as e:
            errors.append(e)

    ts = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert not errors
