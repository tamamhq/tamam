"""Value objects that flow through a harness run — inputs and the final result.

Deliberately plain: the RunSpec is everything the run needs to do its job, the
RunResult is everything it produces. No DB, no framework, no behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel

from tamam.executors.base import AppTarget, ExecutionResult, WebTarget
from tamam.harness.checks import Check
from tamam.harness.verdict import Verdict
from tamam.harness.status import Outcome


@dataclass(frozen=True)
class Budgets:
    max_steps: int
    max_wall_clock_s: int
    stuck_timeout_s: int
    startup_timeout_s: int = 180
    no_progress_limit: int = 4
    repeated_action_limit: int = 6
    repeated_action_total_limit: int = 8

    @classmethod
    def from_settings(cls, settings, *, max_steps: int, max_wall_clock_s: int) -> Budgets:  # noqa: ANN001
        return cls(max_steps=max_steps, max_wall_clock_s=max_wall_clock_s,
                   stuck_timeout_s=settings.stuck_timeout_s,
                   startup_timeout_s=settings.startup_timeout_s,
                   no_progress_limit=settings.no_progress_limit,
                   repeated_action_limit=settings.repeated_action_limit,
                   repeated_action_total_limit=settings.repeated_action_total_limit)


@dataclass(frozen=True)
class RunSpec:
    """Everything a run needs. The executor sees execution_prompt; the judge
    sees the raw instructions; the checks engine sees checks."""

    instructions: str  # raw objective — judge grades against this
    execution_prompt: str  # built prompt — the executor's task
    checks: list[Check]
    checks_exhaustive: bool
    budgets: Budgets
    platform: str = "ios"   # the executor's platform (executors/registry.py)
    target: AppTarget | WebTarget = field(default_factory=AppTarget)   # what is under test
    script: dict | None = None         # test graduation: replay this (executor "script")
    script_status: str | None = None
    script_id: str | None = None


@dataclass
class Timings:
    queue_wait_s: float | None = None
    execute_s: float | None = None
    verify_s: float | None = None


@dataclass
class RunResult:
    """What a HarnessRun produces. The shell persists it; it never persists itself."""

    outcome: Outcome
    reason: str
    disputed: bool = False
    self_verdict: dict | None = None
    judge_verdict: dict | None = None
    evidence: ExecutionResult | None = None  # full bundle — screenshots, video, dom
    usage: dict = field(default_factory=dict)
    timings: Timings = field(default_factory=Timings)

    @classmethod
    def errored(cls, reason: str) -> "RunResult":
        return cls(outcome=Outcome.ERROR, reason=reason)


class JudgeResult(BaseModel):
    """A judge's ruling plus what it cost. Usage flows into RunResult.usage —
    no side-channel dicts."""

    verdict: Verdict
    usage: dict = {}
