"""DbEventSink — the production EventSink: persist step events, update run status,
publish to the live bus.

This is the shell's adapter for the harness's EventSink seam. The core only knows
the protocol (step / lifecycle); all DB and Redis contact is confined here.
"""

from __future__ import annotations

import uuid

from tamam.db import get_session
from tamam.executors.base import StepEvent
from tamam.runtime import events
from tamam.models import Lifecycle, TestRun


class DbEventSink:
    def __init__(self, run_id: uuid.UUID) -> None:
        self._run_id = run_id

    async def step(self, event: StepEvent) -> None:
        await self._set_status(Lifecycle.RUNNING, f"Step {event.step}: {event.next_goal or '…'}")
        await events.append_step_event(self._run_id, event.step, event.model_dump())

    async def lifecycle(self, phase: Lifecycle, message: str) -> None:
        await self._set_status(phase, message)
        await events.publish_lifecycle(self._run_id, phase.value)

    async def _set_status(self, phase: Lifecycle, message: str) -> None:
        async with get_session() as db:
            run = await db.get(TestRun, self._run_id)
            if run is not None:
                run.lifecycle = phase
                run.status_message = message[:500]
                await db.commit()
