"""HarnessRun — the core. One test execution, composed of injected collaborators.

    HarnessRun(spec, executor=…, guardrails=[…], verifier=…, events=…)

It orchestrates the outer loop (run under guardrails → verify → assemble result)
and owns guardrail enforcement itself — it does not call out to a guardrail
function. It touches no DB and no framework: give it fakes and it runs in memory.
"""

from __future__ import annotations

import asyncio
from typing import Sequence

from tamam.executors.base import ExecutionResult, ExecutionSpec, Executor, StepEvent
from tamam.harness.contracts import Clock, EventSink, Guardrail, RunState, SystemClock
from tamam.harness.guardrails import guardrail_error
from tamam.harness.spec import RunResult, RunSpec, Timings
from tamam.harness.verification import Verifier
from tamam.harness.status import Lifecycle
from tamam.observability import span

WATCHDOG_INTERVAL_S = 1.0
# After a guardrail trips inside a step (on_event raised into the executor), the executor gets this
# long to stop its device and return the evidence it has. Only then does the watchdog cancel it:
# cancelling mid-cleanup threw the steps, video and trace away.
CLEANUP_GRACE_S = 60.0


class _GuardrailTripped(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


# Public name: executors that catch broad exceptions must let a guardrail trip through.
GuardrailTripped = _GuardrailTripped


class HarnessRun:
    def __init__(
        self,
        *,
        spec: RunSpec,
        executor: Executor,
        guardrails: Sequence[Guardrail],
        verifier: Verifier,
        events: EventSink,
        clock: Clock = SystemClock(),
    ) -> None:
        self._spec = spec
        self._executor = executor
        self._guardrails = guardrails
        self._verifier = verifier
        self._events = events
        self._clock = clock

    async def execute(self) -> RunResult:
        await self._events.lifecycle(Lifecycle.RUNNING, "Running test")
        with span("harness.execute", executor=self._executor.executor_id):
            evidence = await self._run_guarded()

        await self._events.lifecycle(Lifecycle.JUDGING, "Verifying result")
        t_verify = self._clock.monotonic()
        with span("harness.verify"):
            verdicts = await self._verifier.verify(self._spec, evidence)

        return RunResult(
            outcome=verdicts.resolution.outcome,
            reason=verdicts.resolution.reason,
            disputed=verdicts.resolution.disputed,
            self_verdict=evidence.self_verdict.model_dump() if evidence.self_verdict else None,
            judge_verdict=verdicts.judge.model_dump() if verdicts.judge else None,
            evidence=evidence,
            usage={**evidence.usage.model_dump(), **verdicts.judge_usage},
            timings=Timings(
                execute_s=evidence.usage.duration_s,
                verify_s=round(self._clock.monotonic() - t_verify, 2),
            ),
        )

    # ── guardrail enforcement: owned by the run, driven by injected policies ──

    async def _run_guarded(self) -> ExecutionResult:
        clock, budgets = self._clock, self._spec.budgets
        now = clock.monotonic()
        state = RunState(started_at=now, last_event_at=now, steps_seen=0, now=now)
        breach: list[str] = []
        tripped_at: list[float] = []   # when on_event raised into the executor

        async def on_event(event: StepEvent) -> None:
            state.now = clock.monotonic()
            state.last_event_at = state.now
            state.steps_seen += 1
            # Track no-effect actions: an unchanged screen signature vs the prior step
            # means the last action did nothing (feeds the NoProgress guardrail).
            sig = event.screen_signature
            if sig is not None:
                state.no_progress_steps = (
                    state.no_progress_steps + 1 if sig == state.last_signature else 0
                )
                state.last_signature = sig
            # Fixation: consecutive identical action fingerprints (robust to screen churn).
            # A legitimately repeat-heavy flow (granular undo, "load more", a stepper) should
            # NOT be papered over here with a screen-change heuristic — screen-churn is
            # indistinguishable from real progress by signature alone (a highlight/banner
            # also churns it), so exempting screen-changing repeats would defeat this
            # guardrail. Such flows instead raise their own budget via the per-spec
            # repeated-action limit (see ExecutionSpec / RunIn) rather than weakening the rule.
            fp = event.action_fingerprint
            if fp is not None:
                state.repeat_count = (
                    state.repeat_count + 1 if fp == state.last_fingerprint else 1
                )
                state.last_fingerprint = fp
                state.action_counts[fp] = state.action_counts.get(fp, 0) + 1
            await self._events.step(event)
            reason = self._first_breach(state)
            if reason:
                breach.append(reason)
                tripped_at.append(clock.monotonic())
                raise _GuardrailTripped(reason)

        async def heartbeat() -> None:
            # Liveness between a step's slow calls: reset the idle clock WITHOUT counting
            # a step. A slow-but-live step stays "active"; only genuine silence trips the
            # watchdog. Does not touch startup (steps_seen stays 0 until on_event).
            state.now = clock.monotonic()
            state.last_event_at = state.now

        async def watchdog(task: asyncio.Task) -> None:
            while not task.done():
                await asyncio.sleep(WATCHDOG_INTERVAL_S)
                state.now = clock.monotonic()
                if tripped_at:
                    # The executor already knows (its step raised); let it wrap up — bounded.
                    if state.now - tripped_at[0] >= CLEANUP_GRACE_S:
                        task.cancel()
                        return
                    continue
                reason = self._first_breach(state)
                if reason:
                    breach.append(reason)
                    task.cancel()
                    return

        exec_spec = ExecutionSpec(
            instructions=self._spec.execution_prompt,
            target=self._spec.target,
            platform=self._spec.platform,
            max_steps=budgets.max_steps,
            max_wall_clock_s=budgets.max_wall_clock_s,
            script=self._spec.script,
            script_status=self._spec.script_status,
            script_id=self._spec.script_id,
        )
        exec_task = asyncio.ensure_future(
            self._executor.execute(exec_spec, on_event, heartbeat))
        watchdog_task = asyncio.ensure_future(watchdog(exec_task))
        try:
            result = await exec_task
        except _GuardrailTripped as exc:
            result = ExecutionResult(error=guardrail_error(exc.reason))
        except asyncio.CancelledError:
            reason = guardrail_error(breach[0]) if breach else "Run aborted"
            result = ExecutionResult(error=reason)
        except Exception as exc:  # infra failure from the executor itself
            result = ExecutionResult(error=str(exc))
        finally:
            watchdog_task.cancel()

        # A breach is the harness's own finding: it names the run's error even when the executor
        # caught the trip (or the cancellation) and reported it in its own words.
        if breach:
            result = result.model_copy(update={"error": guardrail_error(breach[0])})
        result.usage.duration_s = round(clock.monotonic() - state.started_at, 2)
        return result

    def _first_breach(self, state: RunState) -> str | None:
        for guardrail in self._guardrails:
            reason = guardrail.evaluate(state)
            if reason:
                return reason
        return None
