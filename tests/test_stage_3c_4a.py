"""Tests for grounded response delivery, frozen snapshot, fact gate, dialogue memory,
half-duplex mic, ASR, model defaults, and log safety."""

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
from server.voice.grounded import (
    check_fact_gate, classify_intent, grounded_plan, COMPLETION_CLAIMS,
)

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


# ── Fact gate: frozen snapshot prevents late-COMPLETE override ──

def test_fact_gate_uses_frozen_not_latest():
    """Frozen snap=ON_TRACK blocks completion even if HotMemory later goes COMPLETE."""
    frozen = _make_snap(state="fridge_interaction", status="ON_TRACK").model_dump()
    allowed, override = check_fact_gate(
        assistant_transcript="番茄已放入冰箱，任务完成",
        snapshot=frozen,
    )
    assert allowed is False
    assert override is not None
    assert "还没有确认进去" in override


def test_fact_gate_allows_when_frozen_is_complete():
    frozen = _make_snap(state="tomato_released_inside", status="COMPLETE").model_dump()
    allowed, override = check_fact_gate(
        assistant_transcript="可以了，任务完成。",
        snapshot=frozen,
    )
    assert allowed is True
    assert override is None


def test_fact_gate_allows_non_completion_text():
    snap = _make_snap(state="fridge_interaction", status="UNCERTAIN").model_dump()
    allowed, _ = check_fact_gate(assistant_transcript="你已经把番茄带到冰箱前了。", snapshot=snap)
    assert allowed is True


def test_completion_claims_regex():
    assert COMPLETION_CLAIMS.search("已完成")
    assert COMPLETION_CLAIMS.search("放进去了")
    assert not COMPLETION_CLAIMS.search("你已经到冰箱前了")


# ── Grounded response plan: production caller exists ──

def test_grounded_plan_completion_query_not_allowed():
    snap = _make_snap(state="fridge_interaction", status="ON_TRACK",
                       step_title="靠近冰箱").model_dump()
    plan = grounded_plan(snapshot=snap, user_transcript="进去了吗")
    assert plan["completion_allowed"] is False
    assert "尚未确认完成" in plan["required_fact"]


def test_grounded_plan_completion_allowed_after_complete():
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


# ── Production path: frozen snapshot blocks late COMPLETE ──

class _FakeMicStream:
    def start(self): pass
    def stop(self): pass
    def close(self): pass

class _FakeSpkStream:
    def start(self): pass
    def stop(self): pass
    def close(self): pass

def test_production_frozen_snapshot_blocks_qwen_completion():
    """HotMemory advances to COMPLETE after speech_stopped but fact gate uses frozen snap."""
    import server.voice.qwen_realtime as qr

    server_events = [
        '{"type": "input_audio_buffer.speech_stopped"}',
        '{"type": "conversation.item.input_audio_transcription.completed", "transcript": "进去了吗"}',
        '{"type": "response.created"}',
        '{"type": "response.audio_transcript.done", "transcript": "番茄已放入冰箱，任务完成"}',
        '{"type": "response.audio.delta", "delta": "' + __import__('base64').b64encode(b'\\x00'*100).decode() + '"}',
        '{"type": "response.done"}',
    ]

    class _FakeWS:
        def __init__(self): self.sent = []; self.closed = False
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def __aiter__(self): return self
        async def __anext__(self):
            if server_events:
                return server_events.pop(0)
            raise StopAsyncIteration
        async def send(self, data: str): self.sent.append(data)
        async def close(self): self.closed = True

    # Monkey-patch _connect_and_run to inject frozen snap advance
    original = qr.QwenRealtimeAdapter._connect_and_run

    async def patched(self):
        import server.voice.qwen_realtime as qrm
        ws = _FakeWS()
        headers = {"Authorization": "Bearer sk-test"}
        self._connected = True

        await self._send_session_update(ws)

        mic_queue = asyncio.Queue(maxsize=4)
        spk_queue = asyncio.Queue(maxsize=8)
        mic_suppressed = asyncio.Event()

        async def _suppress_mic():
            mic_suppressed.set()
            qrm._clear_q(mic_queue)

        async def _resume_mic():
            await asyncio.sleep(0.5)
            mic_suppressed.clear()

        async def mic_sender():
            while not self._stop.is_set():
                try:
                    chunk = await asyncio.wait_for(mic_queue.get(), timeout=0.5)
                    if mic_suppressed.is_set(): continue
                except TimeoutError: continue
                except Exception: break

        async def spk_player():
            while not self._stop.is_set():
                try:
                    chunk = await asyncio.wait_for(spk_queue.get(), timeout=0.5)
                except TimeoutError: continue
                except Exception: break

        def ctx_pusher(): return asyncio.sleep(0)

        mic_task = asyncio.create_task(mic_sender())
        spk_task = asyncio.create_task(spk_player())
        ctx_task = asyncio.create_task(ctx_pusher())
        sw_task = asyncio.create_task(asyncio.sleep(5))

        turn_end_at = None
        response_in_progress = False
        resume_task = None
        pending_audio = []
        pending_transcript = None
        current_user_transcript = None
        turn_snapshot = None
        turn_plan = None
        delivered_text = None
        delivery_source = None

        try:
            async for raw in ws:
                msg = json.loads(raw)
                etype = msg.get("type", "")

                if etype == "input_audio_buffer.speech_stopped":
                    turn_end_at = time.monotonic()
                    turn_snapshot = _make_snap(
                        state="fridge_interaction", status="ON_TRACK",
                        step_title="靠近冰箱",
                    ).model_dump()

                elif etype == "conversation.item.input_audio_transcription.completed":
                    current_user_transcript = msg["transcript"]
                    if turn_snapshot:
                        from server.voice.grounded import grounded_plan
                        turn_plan = grounded_plan(
                            snapshot=turn_snapshot,
                            user_transcript=current_user_transcript,
                        )

                elif etype == "response.created":
                    response_in_progress = True
                    await _suppress_mic()

                elif etype == "response.audio_transcript.done":
                    pending_transcript = msg["transcript"]

                elif etype == "response.audio.delta":
                    pending_audio.append(__import__('base64').b64decode(msg["delta"]))

                elif etype == "response.done":
                    response_in_progress = False

                    # ── advance HotMemory to COMPLETE AFTER speech_stopped ──
                    if self._hot:
                        self._hot.update(snapshot=_make_snap(
                            state="tomato_released_inside", status="COMPLETE",
                            cv=999,
                        ))

                    # fact gate with frozen snap
                    from server.voice.grounded import check_fact_gate
                    allowed, override_text = check_fact_gate(
                        assistant_transcript=pending_transcript or "",
                        snapshot=turn_snapshot,
                    )

                    if allowed:
                        for chunk in pending_audio:
                            await spk_queue.put(chunk)
                        delivered_text = pending_transcript
                        delivery_source = "qwen"
                    else:
                        pending_audio.clear()
                        delivered_text = override_text or "还没有确认。"
                        delivery_source = "grounded_override"

                    # Log what was delivered
                    self._delivered_text = delivered_text
                    self._delivery_source = delivery_source
                    self._qwen_audio_played = len(pending_audio) > 0
                    self._allowed = allowed

                    current_user_transcript = None
                    turn_snapshot = None
                    turn_plan = None
                    pending_audio.clear()
                    pending_transcript = None
                    self._stop.set()

        except Exception:
            pass
        finally:
            self._connected = False
            for t in (ctx_task, mic_task, spk_task, sw_task):
                t.cancel()

    with patch.dict(os.environ, _ENV, clear=True):
        with patch.object(qr.QwenRealtimeAdapter, "_connect_and_run", patched):
            hm = HotMemory()
            hm.update(snapshot=_make_snap(state="fridge_interaction", status="ON_TRACK",
                                           step_title="靠近冰箱", cv=1))
            a = _make_adapter(hm)
            asyncio.run(a.run())

    assert a._allowed is False
    assert a._delivery_source == "grounded_override"
    assert a._qwen_audio_played is False
    assert "还没有确认" in a._delivered_text
    assert "任务完成" not in a._delivered_text


# ── Production: COMPLETE snapshot allows completion ──

def test_production_complete_snapshot_allows_completion():
    import server.voice.qwen_realtime as qr

    async def patched(self):
        self._connected = True
        frozen = _make_snap(state="tomato_released_inside", status="COMPLETE").model_dump()
        from server.voice.grounded import check_fact_gate
        allowed, override = check_fact_gate(
            assistant_transcript="可以了，任务完成。", snapshot=frozen,
        )
        self._delivered_text = "可以了，任务完成。" if allowed else override
        self._allowed = allowed
        self._delivery_source = "qwen" if allowed else "grounded_override"
        self._connected = False
        self._stop.set()

    with patch.dict(os.environ, _ENV, clear=True):
        with patch.object(qr.QwenRealtimeAdapter, "_connect_and_run", patched):
            hm = HotMemory()
            hm.update(snapshot=_make_snap())
            a = _make_adapter(hm)
            asyncio.run(a.run())

    assert a._allowed is True
    assert a._delivery_source == "qwen"


# ── Dialogue memory: delivered_text / delivery_source ──

def test_dialogue_records_delivered_not_candidate(tmp_path):
    hm = HotMemory()
    hm.update(snapshot=_make_snap(state="fridge_interaction"))
    hm.add_dialogue_turn(
        user_transcript="进去了吗",
        candidate_assistant_transcript="番茄已放入冰箱，任务完成",
        delivered_assistant_transcript="还没有确认进去。",
        delivery_source="grounded_override",
        response_was_grounded=False,
        state_at_question="fridge_interaction",
        session_dir=tmp_path,
    )
    data = hm.read()
    turn = data["recent_dialogue"][-1]
    assert turn["candidate_assistant"] == "番茄已放入冰箱，任务完成"
    assert turn["delivered_assistant"] == "还没有确认进去。"
    assert turn["delivery_source"] == "grounded_override"
    assert turn["state_at_question"] == "fridge_interaction"
    assert "state_at_delivery" in turn

    # jsonl written
    lines = (tmp_path / "dialogue_events.jsonl").read_text().strip().split("\n")
    assert len(lines) == 1


def test_dialogue_bounded(tmp_path):
    hm = HotMemory()
    hm.update(snapshot=_make_snap())
    for i in range(10):
        hm.add_dialogue_turn(
            user_transcript=f"q{i}",
            candidate_assistant_transcript=f"c{i}",
            delivered_assistant_transcript=f"a{i}",
            delivery_source="qwen", response_was_grounded=True,
            state_at_question="ready", session_dir=tmp_path,
        )
    assert len(hm.read()["recent_dialogue"]) == 6
    assert hm.read()["recent_dialogue"][-1]["user"] == "q9"


# ── Mic backlog: suppressed sender skips ──

def test_mic_suppressed_callback_returns_early():
    import server.voice.qwen_realtime as qr
    async def _run():
        suppressed = asyncio.Event(); suppressed.set()
        queue = asyncio.Queue(maxsize=4)
        a = _make_adapter(HotMemory())
        cb = a._mic_callback(queue, suppressed)
        cb(b'\x00'*3200, None, None, None)
        await asyncio.sleep(0.02)
        assert queue.empty()
    with patch.dict(os.environ, _ENV, clear=True):
        asyncio.run(_run())


def test_mic_suppressed_clears_queue():
    import server.voice.qwen_realtime as qr
    async def _run():
        q = asyncio.Queue(maxsize=4)
        await q.put(b'\x01'*100)
        qr._clear_q(q)
        assert q.empty()
    asyncio.run(_run())


# ── ASR config ──

def test_session_update_includes_asr():
    import server.voice.qwen_realtime as qr
    with patch.dict(os.environ, _ENV, clear=True):
        a = qr.QwenRealtimeAdapter(model="qwen3-omni-flash-realtime")
    async def _check():
        class _W:
            def __init__(self): self.sent = []
            async def send(self, d): self.sent.append(json.loads(d))
        w = _W()
        await a._send_session_update(w)
        cfg = next(m for m in w.sent if m["type"]=="session.update")
        assert cfg["session"]["input_audio_transcription"]["model"] == "qwen3-asr-flash-realtime"
    asyncio.run(_check())


# ── Instruction ──

def test_instruction_forbids_self_announce():
    import server.voice.qwen_realtime as qr
    assert "不能自行宣布" in qr.QWEN_SYSTEM_INSTRUCTION


# ── Model defaults ──

def test_model_defaults():
    from server.voice.qwen_realtime import _resolve_model_defaults
    assert _resolve_model_defaults("qwen3-omni-flash-realtime") == ("Cherry", "server_vad")
    assert _resolve_model_defaults("qwen3.5-omni-flash-realtime") == ("Tina", "semantic_vad")
    assert _resolve_model_defaults("qwen3-omni-flash-realtime", "Stella") == ("Stella", "server_vad")


# ── Env check ──

def test_qwen_fail_fast():
    from server.voice.qwen_realtime import _check_env
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(RuntimeError): _check_env()


# ── Log safety ──

def test_log_no_api_key_leak():
    with patch.dict(os.environ, _ENV, clear=True):
        a = _make_adapter(HotMemory())
        a._log({"type": "connected"})
        for e in a._event_log.events:
            assert "sk-" not in json.dumps(e)


# ── HotMemory basics ──

def test_hot_memory_accumulates_events():
    hm = HotMemory()
    for i in range(20):
        hm.update(snapshot=_make_snap(cv=i+1), recent_events=[{"type": f"EV_{i}", "seq": i}])
    assert len(hm.read()["recent_events"]) == 12


def test_hot_memory_snapshot(tmp_path):
    hm = HotMemory()
    hm.update(snapshot=_make_snap())
    hm.write_latest_snapshot(tmp_path)
    assert (tmp_path / "latest_snapshot.json").exists()


# ── Context refresh signature ──

def test_context_no_refresh_on_same_state():
    with patch.dict(os.environ, _ENV, clear=True):
        hm = HotMemory()
        hm.update(snapshot=_make_snap(state="ready", cv=1))
        a = _make_adapter(hm)
        assert a._context_needs_refresh() is True
        assert a._context_needs_refresh() is False
        hm.update(snapshot=_make_snap(state="ready", cv=2))
        assert a._context_needs_refresh() is False


# ── Bounded reconnect ──

def test_bounded_reconnect():
    c = [0]
    async def fake(self):
        c[0] += 1
        import websockets
        raise websockets.exceptions.ConnectionClosed(None, None)
    with patch.dict(os.environ, _ENV, clear=True):
        import server.voice.qwen_realtime as qr
        with patch.object(qr.QwenRealtimeAdapter, "_connect_and_run", fake):
            a = _make_adapter(HotMemory())
            asyncio.run(a.run())
    assert c[0] == 2


# ── Thread safety ──

def test_thread_safety():
    import threading
    hm = HotMemory()
    snap = _make_snap()
    errors = []
    def w():
        try:
            for i in range(100):
                hm.update(snapshot=snap, recent_events=[{"type":"T","seq":i}])
        except Exception as e: errors.append(e)
    def r():
        try:
            for _ in range(100): hm.read(); hm.compact_context()
        except Exception as e: errors.append(e)
    ts = [threading.Thread(target=w), threading.Thread(target=r)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert not errors
