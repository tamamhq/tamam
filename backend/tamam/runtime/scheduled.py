"""Scheduled runs: start a run for each configured test once a day (TAMAM_SCHEDULED_TESTS).

Used by the fast-mode shadow: replay can only be compared with the AI on runs that
happen, and the shadow tests rarely run on their own. A test that still has a run in flight
is skipped (no pile-up), and an unknown id is ignored with a warning.
"""

from __future__ import annotations

import logging
import uuid

from sqlmodel import select

from tamam.config import get_settings
from tamam.db import get_session
from tamam.dispatch import enqueue_run
from tamam.models import Lifecycle, Test, TestRun

logger = logging.getLogger(__name__)


def scheduled_test_ids() -> list[uuid.UUID]:
    out = []
    for part in (get_settings().scheduled_tests or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(uuid.UUID(part))
        except ValueError:
            logger.warning("scheduled tests: %r is not a test id", part)
    return out


async def start_scheduled_runs() -> list[str]:
    """Create and enqueue one run per scheduled test; return the new run ids."""
    s = get_settings()
    started: list[str] = []
    for tid in scheduled_test_ids():
        async with get_session() as db:
            if await db.get(Test, tid) is None:
                logger.warning("scheduled tests: no test %s", tid)
                continue
            busy = (await db.execute(select(TestRun.id).where(
                TestRun.test_id == tid, TestRun.lifecycle != Lifecycle.FINISHED))).first()
            if busy is not None:
                logger.info("scheduled tests: %s still has a run in flight; skipped", tid)
                continue
            run = TestRun(test_id=tid, spec_snapshot={
                "max_steps": s.default_max_steps, "max_wall_clock_s": s.default_max_wall_clock_s,
                "scheduled": True})
            db.add(run)
            await db.commit()
            await db.refresh(run)
        await enqueue_run(str(run.id))
        started.append(str(run.id))
    if started:
        logger.info("scheduled tests: started %d run(s)", len(started))
    return started
