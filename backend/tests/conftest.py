from __future__ import annotations

import asyncio
import uuid

import pytest

from tamam.executors.base import WebTarget
from tamam.config import get_settings
from tamam.executors.base import ExecutionResult, ExecutionSpec, OnEvent, StepEvent


@pytest.fixture(autouse=True)
async def fresh_db(tmp_path, monkeypatch):
    """Isolated SQLite DB per test; inline dispatch (no Redis)."""
    monkeypatch.setenv("TAMAM_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/test.db")
    # Empty overrides any TAMAM_REDIS_URL in backend/.env (pydantic-settings reads it too).
    monkeypatch.setenv("TAMAM_REDIS_URL", "")
    # Artifacts land in the test's tmp dir, not the repo — and the storage layer is
    # cached, so reset it to pick up this root (and the local-only backend).
    monkeypatch.setenv("TAMAM_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("TAMAM_STORAGE_BACKEND", "local")
    monkeypatch.setenv("TAMAM_VERDICT_SETTLE_S", "0")   # no real waits in unit tests
    get_settings.cache_clear()

    from tamam.storage import get_storage

    get_storage.cache_clear()

    import tamam.runtime.events as events_module

    events_module._bus = None  # the bus caches its backend globally — reset it

    import tamam.db as db_module

    await db_module.dispose_db()
    from tamam.db import init_db

    await init_db()
    yield
    await db_module.dispose_db()
    get_settings.cache_clear()
    get_storage.cache_clear()


# ── Fakes for every injected seam — the core runs with zero DB / network ──

class FakeExecutor:
    """Scripted executor: emits given events, returns a canned evidence bundle."""

    executor_id = "fake"


    def __init__(
        self,
        result: ExecutionResult | None = None,
        events: list[StepEvent] | None = None,
        delay_s: float = 0,
        hang_after_events: bool = False,
        preserve_on_cancel: bool = False,
    ) -> None:
        self.result = result or ExecutionResult()
        self.events = events or []
        self.delay_s = delay_s
        self.hang_after_events = hang_after_events
        # Mirror the real executors: on guardrail cancel, return partial evidence
        # instead of letting the cancel discard it.
        self.preserve_on_cancel = preserve_on_cancel

    async def execute(self, spec: ExecutionSpec, on_event: OnEvent, heartbeat=None) -> ExecutionResult:
        for event in self.events:
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            await on_event(event)
        if self.hang_after_events:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                if self.preserve_on_cancel:
                    return self.result
                raise
        return self.result


class FakeJudge:
    """Injected in place of AnthropicJudge — no API key, deterministic ruling."""

    def __init__(self, passed: bool = True, reason: str = "ok", usage: dict | None = None) -> None:
        self._passed = passed
        self._reason = reason
        self._usage = usage or {}

    async def evaluate(self, instructions: str, result: ExecutionResult):
        from tamam.harness.spec import JudgeResult
        from tamam.harness.verdict import Verdict

        return JudgeResult(
            verdict=Verdict(
                passed=self._passed,
                reason=self._reason,
                repro_steps=[] if self._passed else ["step a", "step b", "step c"],
            ),
            usage=self._usage,
        )


class FakeEventSink:
    """Collects what the run emits, so tests can assert on the stream."""

    def __init__(self) -> None:
        self.steps: list[StepEvent] = []
        self.phases: list[tuple] = []

    async def step(self, event: StepEvent) -> None:
        self.steps.append(event)

    async def lifecycle(self, phase, message: str) -> None:
        self.phases.append((phase, message))


# ── Core-test builders: assemble a HarnessRun from injected fakes ──

@pytest.fixture
def make_spec():
    from tamam.harness.checks import Check
    from tamam.harness.spec import Budgets, RunSpec

    def _make(
        *,
        checks: list[dict] | None = None,
        checks_exhaustive: bool = False,
        instructions: str = "verify the demo page works",
        max_steps: int = 25,
        max_wall_clock_s: int = 300,
        stuck_timeout_s: int = 60,
        startup_timeout_s: int = 180,
    ) -> RunSpec:
        return RunSpec(
            platform="web",
            target=WebTarget(start_url="https://x.example"),
            instructions=instructions,
            execution_prompt=f"do: {instructions}",
            checks=[Check.model_validate(c) for c in (checks or [])],
            checks_exhaustive=checks_exhaustive,
            budgets=Budgets(
                max_steps=max_steps,
                max_wall_clock_s=max_wall_clock_s,
                stuck_timeout_s=stuck_timeout_s,
                startup_timeout_s=startup_timeout_s,
            ),
        )

    return _make


@pytest.fixture
def build_run(make_spec):
    from tamam.harness.guardrails import (
        NoProgress,
        RepeatedAction,
        StepLimit,
        StuckWatchdog,
        WallClock,
    )
    from tamam.harness.run import HarnessRun
    from tamam.harness.verification import Verifier

    def _build(
        *,
        spec=None,
        executor=None,
        judge=None,
        guardrails=None,
        events=None,
    ) -> HarnessRun:
        spec = spec or make_spec()
        b = spec.budgets
        default_guards = [
            StepLimit(b.max_steps),
            WallClock(b.max_wall_clock_s),
            StuckWatchdog(b.stuck_timeout_s, b.startup_timeout_s),
            NoProgress(b.no_progress_limit),
            RepeatedAction(b.repeated_action_limit, b.repeated_action_total_limit),
        ]
        return HarnessRun(
            spec=spec,
            executor=executor or FakeExecutor(),
            guardrails=default_guards if guardrails is None else guardrails,
            verifier=Verifier(judge=judge or FakeJudge()),
            events=events if events is not None else FakeEventSink(),
        )

    return _build


# ── Shell-test builders: create rows, run the real run_session (deterministic) ──

@pytest.fixture
def make_run():
    async def _make(
        checks: list[dict] | None = None,
        checks_exhaustive: bool = False,
        instructions: str = "verify the demo page works",
        spec_snapshot: dict | None = None,
    ):
        from tamam.db import get_session
        from tamam.models import Project, Test, TestRun

        async with get_session() as db:
            project = Project(org_id="org_test", name="demo")
            db.add(project)
            await db.flush()
            test = Test(
                project_id=project.id,
                name="t",
                instructions=instructions,
                start_url="https://example.com",
                checks=checks or [],
                checks_exhaustive=checks_exhaustive,
            )
            db.add(test)
            await db.flush()
            run = TestRun(test_id=test.id, spec_snapshot=spec_snapshot or {})
            db.add(run)
            await db.commit()
            return project.id, test.id, run.id

    return _make


@pytest.fixture
def run_shell(monkeypatch):
    """Run the real shell (run_session) with a fake executor patched in."""

    async def _run(run_id: uuid.UUID, fake: FakeExecutor):
        import tamam.runtime.session as session_module

        monkeypatch.setattr(session_module, "get_executor", lambda executor_id: fake)
        from tamam.runtime.session import run_session

        await run_session(str(run_id))
        from tamam.db import get_session
        from tamam.models import TestRun

        async with get_session() as db:
            return await db.get(TestRun, run_id)

    return _run


@pytest.fixture(autouse=True)
def _no_ios_settle_grace(monkeypatch):
    """The post-action settle grace is a real-time pause; hermetic tests don't wait.
    Tests of the settle behaviour itself set it back explicitly."""
    import tamam.executors.ios.executor as ios_executor
    monkeypatch.setattr(ios_executor, "_SETTLE_S", 0)
