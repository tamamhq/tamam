"""Outcome resolution — deterministic code, never an LLM.

Order (code-before-prompt):
1. Infra error → ERROR (never conflated with a failing test)
2. Any deterministic check failed → FAILED, reason cites the rule
3. Checks exhaustive and all passed → PASSED, judge skipped
4. Otherwise → escalate (return None); the judge decides.

resolve_pre_judge returns None to mean "escalate" — an explicit absence, not a
boolean flag threaded through a mutable object.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field

from tamam.executors.base import ExecutionResult
from tamam.harness.checks import ChecksResult
from tamam.harness.status import Outcome


class Verdict(BaseModel):
    """The authoritative verdict — also the judge's structured-output shape."""

    passed: bool
    reason: str
    reasoning: str = ""
    repro_steps: list[str] = Field(default_factory=list)
    confidence: float | None = None


@dataclass(frozen=True)
class Resolution:
    outcome: Outcome
    reason: str
    disputed: bool = False


def resolve_pre_judge(
    result: ExecutionResult, checks: ChecksResult, exhaustive: bool
) -> Resolution | None:
    """Everything decidable without an LLM. None → the judge must rule."""
    if result.error:
        return Resolution(outcome=Outcome.ERROR, reason=result.error)

    if checks.outcomes and not checks.all_passed:
        return Resolution(outcome=Outcome.FAILED, reason=checks.failure_reason())

    if checks.outcomes and checks.all_passed and exhaustive:
        return Resolution(
            outcome=Outcome.PASSED,
            reason="All deterministic checks passed (exhaustive — judge skipped)",
        )

    return None


def resolve_without_judge(result: ExecutionResult) -> Resolution:
    """The verdict when no judge is configured (the default since 2026-09-28): the agent's
    own conclusion, after every deterministic check has already passed.

    The agent may only PASS through the harness's stop gate (every checklist clause cited
    with evidence, required actions performed), so its verdict is checked in code, not
    taken on trust. A run that ended WITHOUT the agent concluding (step budget, guardrail)
    is FAILED if a clause was already observed to fail, and otherwise ERROR: nothing
    decided it, and guessing from partial evidence is exactly what the judge got wrong."""
    sv = result.self_verdict
    clauses = result.clauses or []
    if sv is not None and not sv.passed and sv.blocked:
        # "I couldn't do it" is not "the app is broken": a false alarm is the second-worst
        # outcome after a false pass, so a blocked run is inconclusive.
        return Resolution(outcome=Outcome.ERROR,
                          reason=f"Blocked ({sv.blocked.replace('_', ' ')}): {sv.reason}")
    if sv is not None and sv.passed:
        # A PASS stands only on a checklist that backs it: every clause done, none failed,
        # none marked unsupported. Otherwise the agent insisted past the evidence gates.
        weak = [c for c in clauses if c.get("status") != "done" or c.get("unsupported")]
        if weak:
            names = "; ".join(str(c.get("text", c.get("id")))[:80] for c in weak[:3])
            return Resolution(outcome=Outcome.ERROR, reason=(
                f"Inconclusive: the agent reported a pass, but {len(weak)} checklist "
                f"clause(s) have no on-screen evidence: {names}"))
    if sv is not None:
        outcome = Outcome.PASSED if sv.passed else Outcome.FAILED
        return Resolution(outcome=outcome, reason=sv.reason)
    failed = [c for c in (result.clauses or []) if c.get("status") == "failed"]
    if failed:
        c = failed[0]
        return Resolution(outcome=Outcome.FAILED,
                          reason=f"{c.get('text', c.get('id'))}: {c.get('evidence') or 'failed'}")
    done = sum(1 for c in (result.clauses or []) if c.get("status") == "done")
    total = len(result.clauses or [])
    covered = f" ({done}/{total} checklist clauses evidenced)" if total else ""
    return Resolution(outcome=Outcome.ERROR,
                      reason=f"Inconclusive: the agent did not reach a verdict{covered}")


def resolve_with_judge(judge: Verdict, self_verdict_passed: bool | None) -> Resolution:
    """Judge decides; the executor's self-verdict is compared only for the dispute flag."""
    outcome = Outcome.PASSED if judge.passed else Outcome.FAILED
    disputed = self_verdict_passed is not None and self_verdict_passed != judge.passed
    return Resolution(outcome=outcome, reason=judge.reason, disputed=disputed)
