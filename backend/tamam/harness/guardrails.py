"""Guardrails — structural limits as pure, injectable policies.

A guardrail reads the run state and returns a breach reason or None. It has no
I/O and no async: enforcement (the watchdog, cancellation) is the HarnessRun's
job; a guardrail only decides *whether* a limit is crossed.

Guardrails catch structural failures (runaway loops, hangs, blowouts) → the run
resolves these as outcome=error. Verification catches wrong answers → outcome=
failed. Different layers on purpose.
"""

from __future__ import annotations

from dataclasses import dataclass

from tamam.harness.contracts import RunState


def guardrail_error(reason: str) -> str:
    """Prefix a breach reason the way the run surfaces it in run.status_message."""
    return f"Guardrail: {reason}"


@dataclass(frozen=True)
class StepLimit:
    max_steps: int

    def evaluate(self, state: RunState) -> str | None:
        # Trips only on a step BEYOND the budget. The executor's loop already stops at
        # max_steps; tripping AT max_steps turned a `done` on the final allowed step into
        # an ERROR and discarded the verdict it carried.
        if state.steps_seen > self.max_steps:
            return f"step limit reached ({self.max_steps} steps)"
        return None


@dataclass(frozen=True)
class WallClock:
    max_s: int

    def evaluate(self, state: RunState) -> str | None:
        if state.elapsed_s > self.max_s:
            return f"wall-clock limit reached ({self.max_s}s)"
        return None


@dataclass(frozen=True)
class StuckWatchdog:
    timeout_s: int
    # Provisioning (create the session + upload/install the build + launch WDA) happens
    # BEFORE the first step and can legitimately exceed the mid-run idle budget on a
    # device farm. Judge that phase by its own, larger budget; apply the tight idle rule
    # only once stepping has begun. The wall-clock is the ultimate backstop for a truly
    # dead startup.
    startup_timeout_s: int = 180

    def evaluate(self, state: RunState) -> str | None:
        if state.steps_seen >= 1:
            if state.idle_s > self.timeout_s:
                return f"stuck — no step events for {self.timeout_s}s"
        elif state.idle_s > self.startup_timeout_s:
            return f"stuck — session did not start within {self.startup_timeout_s}s"
        return None


@dataclass(frozen=True)
class NoProgress:
    """Steps ARE being produced, but the screen never changes — a spinning agent
    (e.g. retyping a field that won't accept input). StuckWatchdog can't see this
    (events keep flowing); this trips on repeated no-effect actions."""

    max_steps: int

    def evaluate(self, state: RunState) -> str | None:
        if state.no_progress_steps >= self.max_steps:
            return f"no screen progress for {self.max_steps} consecutive actions"
        return None


@dataclass(frozen=True)
class RepeatedAction:
    """The agent is stuck on one action — either CONSECUTIVELY (pure fixation, which
    NoProgress misses when a highlight/banner churns the signature) or in an
    OSCILLATION (tap X → dismiss overlay → tap X → …), where the same action recurs
    many times total though never twice in a row. Backstop only: the executor nudges
    the agent to stop well before this trips. A genuinely repeat-heavy flow raises its
    own budget via the per-spec limit rather than having this rule weakened for everyone."""

    max_repeats: int          # consecutive identical actions
    max_total: int = 8        # total occurrences of any one action (catches oscillation)

    def evaluate(self, state: RunState) -> str | None:
        if state.repeat_count >= self.max_repeats:
            return f"stuck repeating the same action {state.repeat_count} times"
        if state.action_counts:
            fp, n = max(state.action_counts.items(), key=lambda kv: kv[1])
            if n >= self.max_total:
                return f"stuck retrying the same action {n} times without progress"
        return None


def standard_guardrails(budgets) -> list:  # noqa: ANN001  (harness.spec.Budgets)
    """The guardrails every run gets — dashboard, CLI and evals alike, so evals measure the
    harness users run."""
    return [
        StepLimit(budgets.max_steps),
        WallClock(budgets.max_wall_clock_s),
        StuckWatchdog(budgets.stuck_timeout_s, budgets.startup_timeout_s),
        NoProgress(budgets.no_progress_limit),
        RepeatedAction(budgets.repeated_action_limit, budgets.repeated_action_total_limit),
    ]
