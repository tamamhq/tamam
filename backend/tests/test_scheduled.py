"""Scheduled runs (TAMAM_SCHEDULED_TESTS): one run per test per day, no pile-up, no surprises."""

import uuid

from tamam.config import get_settings
from tamam.db import get_session
from tamam.models import Lifecycle, Project, Test, TestRun


async def _test():
    async with get_session() as db:
        p = Project(org_id="o", name="P"); db.add(p); await db.flush()
        t = Test(project_id=p.id, name="T", instructions="x", executor_id="ios_agent")
        db.add(t); await db.commit()
        return t.id


async def test_nothing_is_scheduled_by_default(monkeypatch):
    from tamam.runtime import scheduled
    monkeypatch.setattr(get_settings(), "scheduled_tests", "")
    assert await scheduled.start_scheduled_runs() == []


async def test_each_scheduled_test_gets_one_run_and_unknown_ids_are_ignored(monkeypatch):
    from tamam.runtime import scheduled
    enqueued = []

    async def fake_enqueue(rid):
        enqueued.append(rid)
    monkeypatch.setattr(scheduled, "enqueue_run", fake_enqueue)
    tid = await _test()
    monkeypatch.setattr(get_settings(), "scheduled_tests", f"{tid}, {uuid.uuid4()}, not-an-id")
    started = await scheduled.start_scheduled_runs()
    assert len(started) == 1 and enqueued == started
    async with get_session() as db:
        run = await db.get(TestRun, uuid.UUID(started[0]))
        assert run.test_id == tid and run.spec_snapshot["scheduled"] is True


async def test_a_test_with_a_run_in_flight_is_skipped(monkeypatch):
    from tamam.runtime import scheduled

    async def fake_enqueue(rid):
        pass
    monkeypatch.setattr(scheduled, "enqueue_run", fake_enqueue)
    tid = await _test()
    async with get_session() as db:
        db.add(TestRun(test_id=tid, lifecycle=Lifecycle.RUNNING)); await db.commit()
    monkeypatch.setattr(get_settings(), "scheduled_tests", str(tid))
    assert await scheduled.start_scheduled_runs() == []
