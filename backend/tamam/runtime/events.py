"""State subsystem: append-only step events + live publish for SSE.

Two bus implementations behind one interface:
- InProcessBus — inline execution mode (no Redis), same-process SSE
- RedisBus    — ARQ worker mode, cross-process SSE via Redis pub/sub
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import AsyncIterator

from tamam.config import get_settings
from tamam.db import get_session
from tamam.models import RunStepEvent

CLOSE = "__close__"


class InProcessBus:
    def __init__(self) -> None:
        self._queues: dict[str, list[asyncio.Queue]] = {}

    async def publish(self, run_id: str, payload: dict) -> None:
        for q in self._queues.get(run_id, []):
            q.put_nowait(payload)

    async def close(self, run_id: str) -> None:
        for q in self._queues.get(run_id, []):
            q.put_nowait(CLOSE)

    async def subscribe(self, run_id: str) -> AsyncIterator[dict]:
        q: asyncio.Queue = asyncio.Queue()
        self._queues.setdefault(run_id, []).append(q)
        try:
            while True:
                item = await q.get()
                if item == CLOSE:
                    return
                yield item
        finally:
            self._queues.get(run_id, []).remove(q)

    async def subscriber_count(self, run_id: str) -> int:
        return len(self._queues.get(run_id, []))


class RedisBus:
    def __init__(self, redis_url: str) -> None:
        self._url = redis_url
        self._redis = None

    def _client(self):
        if self._redis is None:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(self._url)
        return self._redis

    @staticmethod
    def _channel(run_id: str) -> str:
        return f"tamam:run:{run_id}"

    async def publish(self, run_id: str, payload: dict) -> None:
        await self._client().publish(self._channel(run_id), json.dumps(payload, default=str))

    async def close(self, run_id: str) -> None:
        await self._client().publish(self._channel(run_id), CLOSE)

    async def subscribe(self, run_id: str) -> AsyncIterator[dict]:
        pubsub = self._client().pubsub()
        await pubsub.subscribe(self._channel(run_id))
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                data = message["data"]
                data = data.decode() if isinstance(data, bytes) else data
                if data == CLOSE:
                    return
                yield json.loads(data)
        finally:
            await pubsub.unsubscribe(self._channel(run_id))
            await pubsub.aclose()

    async def subscriber_count(self, run_id: str) -> int:
        """Live viewers on this run's channel — the watch-gating signal for live
        preview. Each SSE subscriber is one channel subscriber."""
        res = await self._client().pubsub_numsub(self._channel(run_id))
        return int(res[0][1]) if res else 0


_bus = None


def get_bus():
    global _bus
    if _bus is None:
        settings = get_settings()
        _bus = RedisBus(settings.redis_url) if settings.redis_url else InProcessBus()
    return _bus


async def append_step_event(run_id: uuid.UUID, step: int, payload: dict) -> None:
    """Append-only persistence + live publish."""
    async with get_session() as session:
        session.add(RunStepEvent(run_id=run_id, step=step, payload=payload))
        await session.commit()
    await get_bus().publish(str(run_id), {"type": "step", "step": step, "payload": payload})


async def publish_lifecycle(run_id: uuid.UUID, lifecycle: str, message: str = "") -> None:
    await get_bus().publish(
        str(run_id), {"type": "lifecycle", "lifecycle": lifecycle, "message": message}
    )
