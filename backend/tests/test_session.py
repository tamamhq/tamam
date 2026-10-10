"""Shell (run_session) — it wires the core to the DB and persists the result.

Deterministic / error paths only (no judge), so these run without an API key.
The judge path is covered in test_run.py with a FakeJudge.
"""

import json

from sqlmodel import select

from tamam.db import get_session
from tamam.executors.base import ExecutionResult, SelfVerdict, StepEvent
from tamam.models import Lifecycle, Outcome, RunStepEvent
from tests.conftest import FakeExecutor


async def test_persists_deterministic_pass(make_run, run_shell):
    _, _, run_id = await make_run(
        checks=[{"kind": "url_matches", "value": "/success"}], checks_exhaustive=True
    )
    fake = FakeExecutor(
        events=[StepEvent(step=1, url="https://x.example/checkout")],
        result=ExecutionResult(
            final_url="https://x.example/checkout/success",
            self_verdict=SelfVerdict(passed=True, reason="done"),
        ),
    )
    run = await run_shell(run_id, fake)
    assert run.lifecycle == Lifecycle.FINISHED
    assert run.outcome == Outcome.PASSED
    assert run.judge_verdict is None
    assert run.spec_snapshot.get("prompts_version")
    assert "timings" in run.usage


async def test_persists_infra_error(make_run, run_shell):
    _, _, run_id = await make_run()
    run = await run_shell(run_id, FakeExecutor(result=ExecutionResult(error="Failed to navigate")))
    assert run.outcome == Outcome.ERROR


async def test_persists_rule_failure(make_run, run_shell):
    _, _, run_id = await make_run(checks=[{"kind": "url_matches", "value": "/success"}])
    fake = FakeExecutor(result=ExecutionResult(final_url="https://x.example/cart"))
    run = await run_shell(run_id, fake)
    assert run.outcome == Outcome.FAILED
    assert "url_matches" in run.status_message


async def test_persists_guardrail_halt(make_run, run_shell):
    _, _, run_id = await make_run(spec_snapshot={"max_steps": 2})
    fake = FakeExecutor(
        events=[StepEvent(step=i) for i in range(1, 8)],
        result=ExecutionResult(self_verdict=SelfVerdict(passed=True, reason="done")),
    )
    run = await run_shell(run_id, fake)
    assert run.outcome == Outcome.ERROR
    assert "guardrail" in run.status_message.lower()


async def test_step_events_appended_as_rows(make_run, run_shell):
    _, _, run_id = await make_run(
        checks=[{"kind": "url_matches", "value": "example"}], checks_exhaustive=True
    )
    fake = FakeExecutor(
        events=[StepEvent(step=1, url="https://x.example", next_goal="open"),
                StepEvent(step=2, url="https://x.example/a", next_goal="click")],
        result=ExecutionResult(final_url="https://x.example/a"),
    )
    await run_shell(run_id, fake)
    async with get_session() as db:
        rows = (
            await db.execute(
                select(RunStepEvent).where(RunStepEvent.run_id == run_id).order_by(RunStepEvent.step)
            )
        ).scalars().all()
    assert [r.step for r in rows] == [1, 2]
    assert rows[1].payload["next_goal"] == "click"


async def test_duplicate_execution_is_skipped(make_run, run_shell):
    """ARQ redelivery / double-enqueue must not re-run a run that already left QUEUED."""
    from tamam.models import TestRun
    _, _, run_id = await make_run()
    # Simulate a run already claimed/started by a prior (now-dead) worker.
    async with get_session() as db:
        run = await db.get(TestRun, run_id)
        run.lifecycle = Lifecycle.RUNNING
        run.status_message = "in-flight"
        await db.commit()
    fake = FakeExecutor(result=ExecutionResult(self_verdict=SelfVerdict(passed=True, reason="x")))
    run = await run_shell(run_id, fake)
    # Skipped: the executor never ran, the row is untouched (no overwrite of the verdict).
    assert run.lifecycle == Lifecycle.RUNNING
    assert run.outcome is None
    assert run.status_message == "in-flight"


async def test_cancellation_persists_terminal_error(make_run, monkeypatch):
    """Worker shutdown / job_timeout cancels run_session while it awaits (e.g. during the
    verify/judge phase, which HarnessRun does NOT swallow). It must persist a terminal ERROR
    (never left non-terminal) and still propagate the cancellation."""
    import asyncio
    import uuid as _uuid
    import pytest
    import tamam.runtime.session as session_module
    from tamam.models import TestRun

    _, _, run_id = await make_run()

    class _CancelledRun:
        def __init__(self, *a, **kw):
            pass
        async def execute(self):
            raise asyncio.CancelledError()

    # Stub HarnessRun so execute() raises a bare CancelledError at the run_session level —
    # the exact signal a task cancellation delivers when it lands outside the executor phase.
    monkeypatch.setattr("tamam.assembly.HarnessRun", _CancelledRun)
    monkeypatch.setattr(session_module, "get_executor", lambda executor_id: object())

    with pytest.raises(asyncio.CancelledError):
        await session_module.run_session(str(run_id))

    async with get_session() as db:
        run = await db.get(TestRun, _uuid.UUID(str(run_id)))
    assert run.lifecycle == Lifecycle.FINISHED
    assert run.outcome == Outcome.ERROR
    assert run.finished_at is not None


async def test_reaper_marks_stale_runs_error_but_spares_fresh(make_run):
    """A run stuck non-terminal past the deadline is reaped to ERROR; a recent one is not."""
    from datetime import UTC, datetime, timedelta
    from tamam.models import TestRun
    from tamam.runtime.session import reap_stale_runs

    _, _, stale_id = await make_run()
    _, _, fresh_id = await make_run()
    async with get_session() as db:
        stale = await db.get(TestRun, stale_id)
        stale.lifecycle = Lifecycle.RUNNING
        stale.started_at = datetime.now(UTC) - timedelta(hours=5)
        fresh = await db.get(TestRun, fresh_id)
        fresh.lifecycle = Lifecycle.RUNNING
        fresh.started_at = datetime.now(UTC)
        await db.commit()

    reaped = await reap_stale_runs(max_age_s=3600)
    assert reaped == 1
    async with get_session() as db:
        stale = await db.get(TestRun, stale_id)
        fresh = await db.get(TestRun, fresh_id)
    assert stale.lifecycle == Lifecycle.FINISHED and stale.outcome == Outcome.ERROR
    assert fresh.lifecycle == Lifecycle.RUNNING and fresh.outcome is None


async def test_clauses_and_a_trace_outlive_the_worker(make_run, run_shell):
    """The worker's disk is wiped on deploy; the checklist and the per-step trace are what
    test graduation compiles from, so they are stored with the run."""
    from tamam.storage import get_storage
    _, _, run_id = await make_run()
    clauses = [{"id": "c1", "text": "Open Settings", "status": "done", "step": 2,
                "evidence": "Settings title"}]
    trace = [{"step": 1, "action": {"action": "tap", "ref": "e3"}},
             {"step": 2, "action": {"action": "done", "passed": True}}]
    fake = FakeExecutor(result=ExecutionResult(
        self_verdict=SelfVerdict(passed=True, reason="ok"), clauses=clauses,
        usage_extra={"step_trace": trace}))
    run = await run_shell(run_id, fake)
    assert run.clauses == clauses
    assert run.trace_key == f"runs/{run_id}/step_trace.jsonl"
    lines = (await get_storage().get_bytes(run.trace_key)).decode().splitlines()
    assert [json.loads(x)["step"] for x in lines] == [1, 2]


async def test_a_run_without_a_trace_stores_none(make_run, run_shell):
    _, _, run_id = await make_run()
    run = await run_shell(run_id, FakeExecutor(result=ExecutionResult(error="boom")))
    assert run.trace_key is None and run.clauses is None


async def test_a_secret_learned_late_is_scrubbed_from_already_stored_steps(make_run, run_shell):
    """step 1 streamed "hunter22" before the run knew it was a password (typed into a
    secure field at step 2). The executor returns its steps scrubbed; the stored rows follow."""
    _, _, run_id = await make_run(
        checks=[{"kind": "url_matches", "value": "example"}], checks_exhaustive=True
    )
    fake = FakeExecutor(
        events=[StepEvent(step=1, url="https://x.example", next_goal="use hunter22 to sign in"),
                StepEvent(step=2, url="https://x.example", next_goal="type the password")],
        result=ExecutionResult(final_url="https://x.example", steps=[
            StepEvent(step=1, url="https://x.example", next_goal="use [typed text] to sign in"),
            StepEvent(step=2, url="https://x.example", next_goal="type the password")]),
    )
    await run_shell(run_id, fake)
    async with get_session() as db:
        rows = (await db.execute(select(RunStepEvent).where(RunStepEvent.run_id == run_id)
                                 .order_by(RunStepEvent.step))).scalars().all()
    assert "hunter22" not in json.dumps([r.payload for r in rows])
    assert rows[0].payload["next_goal"] == "use [typed text] to sign in"
