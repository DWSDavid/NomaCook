from __future__ import annotations

import asyncio
import json

from server.realtime import provider as provider_module
from server.realtime.provider import QwenRealtimeProvider


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.incoming: asyncio.Queue[str] = asyncio.Queue()
        self.closed = False

    async def send(self, raw: str) -> None:
        self.sent.append(raw)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if self.closed:
            raise StopAsyncIteration
        return await self.incoming.get()


def test_qwen_ready_waits_for_matching_session_updated(monkeypatch) -> None:
    async def run() -> None:
        fake_ws = _FakeWebSocket()

        async def connect(*args, **kwargs):
            return fake_ws

        monkeypatch.setattr(provider_module.websockets, "connect", connect)
        provider = QwenRealtimeProvider(
            api_key="fake", workspace_id="workspace"
        )
        start_task = asyncio.create_task(
            provider.start(semantic_vad=True, context={"revision": 1})
        )
        await asyncio.sleep(0)
        assert not start_task.done()
        assert json.loads(fake_ws.sent[0])["type"] == "session.update"
        await fake_ws.incoming.put(json.dumps({"type": "session.updated"}))
        await start_task
        ready = await anext(provider.events())
        assert ready.type == "ready"
        await provider.stop()

    asyncio.run(run())
