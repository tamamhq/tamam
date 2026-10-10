"""LivePreview — the frame path for watching a run in real time.

Three properties, by construction:
- watch-gated: `viewers()` reports live SSE subscribers; the executor only runs
  the screencast while someone is watching (zero cost otherwise).
- throttled: frames are published at most `fps` per second, so per-stream
  bandwidth is bounded and predictable regardless of how fast CDP emits.
- off the DB: frames go straight to the pub/sub bus as ephemeral `frame` events;
  they are never persisted (unlike step events).

It rides the bus seam, so it is identical for a local Chromium or a remote CDP
browser — the frames come from the CDP connection either way.
"""

from __future__ import annotations

import time


class LivePreview:
    def __init__(self, run_id, bus, *, fps: int = 4) -> None:
        self._run_id = str(run_id)
        self._bus = bus
        self._interval = 1.0 / fps
        self._last = 0.0

    async def viewers(self) -> int:
        return await self._bus.subscriber_count(self._run_id)

    async def publish_frame(self, b64: str) -> None:
        now = time.monotonic()
        if now - self._last < self._interval:
            return  # throttle: drop frames above the target fps
        self._last = now
        await self._bus.publish(self._run_id, {"type": "frame", "data": b64})
