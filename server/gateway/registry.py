"""Bounded provider_call_id admission and duplicate protection."""

from __future__ import annotations

import asyncio
from collections import deque


class ProviderCallRegistry:
    def __init__(self, *, recent_capacity: int = 1024) -> None:
        if recent_capacity < 1:
            raise ValueError("recent_capacity must be positive")
        self._capacity = recent_capacity
        self._lock = asyncio.Lock()
        self._active: set[str] = set()
        self._recent: deque[str] = deque()
        self._known: set[str] = set()

    async def admit(self, provider_call_id: str) -> bool:
        if not provider_call_id:
            return False
        async with self._lock:
            if provider_call_id in self._active or provider_call_id in self._known:
                return False
            self._active.add(provider_call_id)
            self._known.add(provider_call_id)
            return True

    async def complete(self, provider_call_id: str) -> None:
        async with self._lock:
            self._active.discard(provider_call_id)
            self._recent.append(provider_call_id)
            while len(self._recent) > self._capacity:
                oldest = self._recent.popleft()
                self._known.discard(oldest)

    async def release_active(self, provider_call_id: str) -> None:
        """Release active ownership while retaining duplicate history."""

        async with self._lock:
            self._active.discard(provider_call_id)

    async def is_active(self, provider_call_id: str) -> bool:
        async with self._lock:
            return provider_call_id in self._active
