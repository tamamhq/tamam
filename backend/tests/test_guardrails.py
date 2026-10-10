"""Guardrails at two levels: pure policies, and enforcement inside HarnessRun.

A tripped guardrail always resolves to outcome=error (structural), never failed.
"""

import asyncio

from tamam.executors.base import ExecutionResult, SelfVerdict, StepEvent
from tamam.harness.contracts import RunState
from tamam.harness.guardrails import StepLimit, StuckWatchdog, WallClock
from tamam.models import Outcome
from tests.conftest import FakeExecutor


def _state(steps=0, elapsed=0.0, idle=0.0) -> RunState:
    now = max(elapsed, idle)
    return RunState(started_at=0.0, last_event_at=now - idle, steps_seen=steps, now=now)


def test_step_limit_policy():
    assert StepLimit(3).evaluate(_state(steps=3)) is None  # the last allowed step
    assert StepLimit(3).evaluate(_state(steps=4)) == "step limit reached (3 steps)"


def test_wall_clock_policy():
    assert WallClock(10).evaluate(_state(elapsed=9)) is None
    assert "wall-clock" in WallClock(10).evaluate(_state(elapsed=11))


def test_stuck_policy():
    # mid-run idle rule applies once stepping has begun (steps_seen >= 1)
    assert StuckWatchdog(5).evaluate(_state(steps=1, idle=4)) is None
    assert "stuck" in StuckWatchdog(5).evaluate(_state(steps=1, idle=6))


def test_stuck_startup_grace():
    """Before the first step, provisioning gets the larger startup budget — a slow
    build install doesn't false-trip the mid-run idle rule."""
    wd = StuckWatchdog(5, startup_timeout_s=100)
    assert wd.evaluate(_state(steps=0, idle=50)) is None       # still installing — OK
    assert wd.evaluate(_state(steps=0, idle=90)) is None       # under startup budget
    msg = wd.evaluate(_state(steps=0, idle=120))               # startup itself hung
    assert msg and "did not start" in msg


async def test_step_limit_halts_run(build_run, make_spec):
    run = build_run(
        spec=make_spec(max_steps=2),
        executor=FakeExecutor(
            events=[StepEvent(step=i) for i in range(1, 8)],
            result=ExecutionResult(self_verdict=SelfVerdict(passed=True, reason="done")),
        ),
    )
    result = await run.execute()
    assert result.outcome == Outcome.ERROR
    assert "step limit" in result.reason


async def test_verdict_on_final_allowed_step_is_kept(build_run, make_spec):
    """A `done` on step N of an N-step budget is a verdict, not a guardrail breach."""
    run = build_run(
        spec=make_spec(max_steps=2),
        executor=FakeExecutor(
            events=[StepEvent(step=1), StepEvent(step=2)],
            result=ExecutionResult(self_verdict=SelfVerdict(passed=True, reason="done")),
        ),
    )
    result = await run.execute()
    assert result.outcome != Outcome.ERROR


async def test_wall_clock_halts_run(build_run, make_spec):
    run = build_run(spec=make_spec(max_wall_clock_s=1), executor=FakeExecutor(hang_after_events=True))
    result = await run.execute()
    assert result.outcome == Outcome.ERROR
    assert "wall-clock" in result.reason


async def test_stuck_halts_run(build_run, make_spec):
    # emits a step, THEN hangs → the mid-run idle rule (steps_seen>=1) trips fast
    run = build_run(
        spec=make_spec(max_wall_clock_s=300, stuck_timeout_s=1),
        executor=FakeExecutor(events=[StepEvent(step=1)], hang_after_events=True),
    )
    result = await run.execute()
    assert result.outcome == Outcome.ERROR
    assert "stuck" in result.reason.lower()


async def test_startup_hang_halts_run(build_run, make_spec):
    """A run that never emits a first step (session/install wedged) is caught by the
    startup budget, not left to the wall-clock."""
    run = build_run(
        spec=make_spec(max_wall_clock_s=300, stuck_timeout_s=60, startup_timeout_s=1),
        executor=FakeExecutor(hang_after_events=True),  # zero events, then hang
    )
    result = await run.execute()
    assert result.outcome == Outcome.ERROR
    assert "did not start" in result.reason.lower()


async def test_heartbeat_keeps_watchdog_quiet_during_slow_step(build_run, make_spec):
    """A slow-but-LIVE step (a heavy screen whose driver calls take many seconds) emits
    heartbeats between calls; the stuck-watchdog must not mistake it for a wedged run."""
    import asyncio

    from tamam.executors.base import ExecutionResult, SelfVerdict

    class SlowButLive:
        executor_id = "slow"

        async def execute(self, spec, on_event, heartbeat=None):
            await on_event(StepEvent(step=1))
            for _ in range(12):  # ~2.4s of work — far exceeds stuck_timeout_s=1 below
                await asyncio.sleep(0.2)
                if heartbeat:
                    await heartbeat()
            return ExecutionResult(self_verdict=SelfVerdict(passed=True, reason="done"))

    run = build_run(spec=make_spec(stuck_timeout_s=1), executor=SlowButLive())
    result = await run.execute()
    assert result.outcome != Outcome.ERROR
    assert result.reason is None or "stuck" not in result.reason.lower()


async def test_executor_exception_is_infra_error(build_run):
    class Exploding:
        executor_id = "boom"

        async def execute(self, spec, on_event, heartbeat=None):
            raise RuntimeError("browser disconnected mid-flight")

    result = await build_run(executor=Exploding()).execute()
    assert result.outcome == Outcome.ERROR
    assert "disconnected" in result.reason


async def test_execute_records_duration(build_run, make_spec):
    run = build_run(
        spec=make_spec(checks=[{"kind": "url_matches", "value": "example"}], checks_exhaustive=True),
        executor=FakeExecutor(
            events=[StepEvent(step=1)], result=ExecutionResult(final_url="https://x.example/done")
        ),
    )
    result = await run.execute()
    assert result.timings.execute_s is not None


def test_no_progress_policy():
    from tamam.harness.guardrails import NoProgress

    s = _state()
    s.no_progress_steps = 3
    assert NoProgress(4).evaluate(s) is None
    s.no_progress_steps = 4
    assert "no screen progress" in NoProgress(4).evaluate(s)


async def test_no_progress_halts_run(build_run, make_spec):
    """Events keep flowing (StuckWatchdog can't catch it), but the screen signature
    never changes → the agent is spinning → NoProgress halts as a structural error."""
    from tamam.harness.guardrails import NoProgress

    # 6 events all on the SAME screen — a stalled agent retyping a dead field.
    events = [StepEvent(step=i, screen_signature="SAME") for i in range(1, 7)]
    run = build_run(
        spec=make_spec(),
        executor=FakeExecutor(
            events=events,
            result=ExecutionResult(self_verdict=SelfVerdict(passed=True, reason="done")),
        ),
        guardrails=[NoProgress(3)],
    )
    result = await run.execute()
    assert result.outcome == Outcome.ERROR
    assert "no screen progress" in result.reason


async def test_changing_screens_do_not_trip_no_progress(build_run, make_spec):
    """Distinct signatures each step = real progress → NoProgress never fires."""
    from tamam.harness.guardrails import NoProgress

    events = [StepEvent(step=i, screen_signature=f"screen-{i}") for i in range(1, 7)]
    run = build_run(
        spec=make_spec(),
        executor=FakeExecutor(
            events=events,
            result=ExecutionResult(self_verdict=SelfVerdict(passed=True, reason="ok")),
        ),
        guardrails=[NoProgress(3)],
    )
    result = await run.execute()
    assert result.outcome != Outcome.ERROR


async def test_guardrail_abort_preserves_evidence(build_run, make_spec):
    """When a guardrail aborts a run, the executor's partial evidence (steps, and
    thus screenshots/video) must survive into the RunResult — not be discarded — so
    it can be persisted. Outcome is still ERROR."""
    from tamam.executors.base import ExecutionResult, StepEvent

    ev = ExecutionResult(steps=[StepEvent(step=1, screenshot_path="/tmp/a.png"),
                                StepEvent(step=2, screenshot_path="/tmp/b.png")],
                         video_path="/tmp/v.mp4")
    run = build_run(
        spec=make_spec(max_wall_clock_s=300, stuck_timeout_s=1),
        executor=FakeExecutor(events=[StepEvent(step=1)], result=ev,
                              hang_after_events=True, preserve_on_cancel=True),
    )
    result = await run.execute()
    assert result.outcome == Outcome.ERROR
    assert "stuck" in result.reason.lower()
    assert result.evidence is not None
    assert len(result.evidence.steps) == 2          # evidence preserved
    assert result.evidence.video_path == "/tmp/v.mp4"  # video preserved


def test_repeated_action_policy():
    from tamam.harness.guardrails import RepeatedAction
    s = _state()
    s.repeat_count = 5
    assert RepeatedAction(6).evaluate(s) is None
    s.repeat_count = 6
    assert "repeating the same action" in RepeatedAction(6).evaluate(s)


async def test_repeated_action_halts_run(build_run, make_spec):
    """Identical action fingerprints each step (screen may churn) → fixation → error."""
    from tamam.harness.guardrails import RepeatedAction
    events = [StepEvent(step=i, screen_signature=f"churn-{i}", action_fingerprint="tap:square.grid.2x2:1,5")
              for i in range(1, 8)]
    run = build_run(
        spec=make_spec(),
        executor=FakeExecutor(events=events,
                              result=ExecutionResult(self_verdict=SelfVerdict(passed=True, reason="x"))),
        guardrails=[RepeatedAction(4)],
    )
    result = await run.execute()
    assert result.outcome == Outcome.ERROR
    assert "repeating the same action" in result.reason


async def test_varied_actions_do_not_trip_repeated(build_run, make_spec):
    from tamam.harness.guardrails import RepeatedAction
    events = [StepEvent(step=i, action_fingerprint=f"tap:e{i}:0,{i}") for i in range(1, 8)]
    run = build_run(
        spec=make_spec(),
        executor=FakeExecutor(events=events,
                              result=ExecutionResult(self_verdict=SelfVerdict(passed=True, reason="ok"))),
        guardrails=[RepeatedAction(4)],
    )
    result = await run.execute()
    assert result.outcome != Outcome.ERROR


async def test_oscillation_trips_repeated_action(build_run, make_spec):
    """Alternating actions (tap X / dismiss Y) never repeat consecutively, but the
    same action recurs many times total → RepeatedAction's total-count catches it."""
    from tamam.harness.guardrails import RepeatedAction
    seq = []
    for i in range(1, 12):
        seq.append(StepEvent(step=i, screen_signature=f"c{i}",
                             action_fingerprint="tap:Rage Room:5,40" if i % 2 else "tap:dismiss:9,90"))
    run = build_run(
        spec=make_spec(),
        executor=FakeExecutor(events=seq,
                              result=ExecutionResult(self_verdict=SelfVerdict(passed=True, reason="x"))),
        guardrails=[RepeatedAction(6, 5)],  # consecutive=6 (won't trip), total=5 (will)
    )
    result = await run.execute()
    assert result.outcome == Outcome.ERROR
    assert "same action" in result.reason


class _StopsSlowly:
    """Acts without changing the screen until a guardrail trips, then takes a while to stop
    its device (a real device does) before returning the evidence it gathered."""
    executor_id = "slow_stop"

    def __init__(self, cleanup_s: float):
        self.cleanup_s = cleanup_s

    async def execute(self, spec, on_event, heartbeat=None):
        from tamam.executors.base import ExecutionResult, StepEvent
        steps = []
        try:
            for i in range(1, 20):
                ev = StepEvent(step=i, screen_signature="same", action_fingerprint=f"a{i}")
                steps.append(ev)
                await on_event(ev)
        except Exception as exc:              # the trip arrives as an exception from on_event
            await asyncio.sleep(self.cleanup_s)
            return ExecutionResult(steps=steps, error=f"stopped: {exc}")
        return ExecutionResult(steps=steps)


async def test_a_tripped_run_keeps_its_evidence_while_the_device_stops(build_run, monkeypatch):
    """The watchdog re-checked the still-true breach and cancelled the executor mid-cleanup, so
    a guardrail stop lost its steps, video and trace."""
    from tamam.harness import run as run_mod
    from tamam.harness.guardrails import NoProgress
    monkeypatch.setattr(run_mod, "WATCHDOG_INTERVAL_S", 0.02)
    run = build_run(executor=_StopsSlowly(cleanup_s=0.3), guardrails=[NoProgress(2)])
    result = await run.execute()
    assert result.outcome == Outcome.ERROR
    assert len(result.evidence.steps) == 3          # kept: the trip came at the 3rd unchanged step
    assert result.evidence.error == "Guardrail: no screen progress for 2 consecutive actions"


async def test_an_executor_that_hangs_after_a_trip_is_still_cancelled(build_run, monkeypatch):
    from tamam.harness import run as run_mod
    from tamam.harness.guardrails import NoProgress
    monkeypatch.setattr(run_mod, "WATCHDOG_INTERVAL_S", 0.02)
    monkeypatch.setattr(run_mod, "CLEANUP_GRACE_S", 0.2)
    run = build_run(executor=_StopsSlowly(cleanup_s=30), guardrails=[NoProgress(2)])
    result = await asyncio.wait_for(run.execute(), timeout=5)
    assert result.outcome == Outcome.ERROR and result.evidence.error.startswith("Guardrail:")
