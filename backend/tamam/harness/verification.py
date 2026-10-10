"""The Verifier — the harness's verification subsystem, composed of the checks
engine and an injected Judge.

Deterministic checks run first (zero tokens); the judge is consulted only for
the semantic residue. The judge is a dependency, so tests inject a fake and the
core needs no API key.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tamam.executors.base import ExecutionResult
from tamam.harness.checks import run_checks
from tamam.harness.contracts import Judge
from tamam.harness.spec import RunSpec
from tamam.harness.checks import Check, required_actions
from tamam.harness.verdict import (Resolution, Verdict, resolve_pre_judge, resolve_with_judge,
                                 resolve_without_judge)


@dataclass
class Verdicts:
    resolution: Resolution
    judge: Verdict | None
    judge_usage: dict = field(default_factory=dict)


class Verifier:
    """`judge=None` → no LLM judge: after the deterministic checks, the agent's own
    verdict (which could only pass through the evidence-gated stop gate) decides. That is
    the default: measured, the judge overruled correct verdicts and never fixed a wrong one."""

    def __init__(self, judge: Judge | None, planner=None) -> None:  # noqa: ANN001
        self._judge = judge
        self._planner = planner  # AutoPlanner | None — autonomous deterministic checks

    @property
    def source(self) -> str:
        """Who decides verdicts the checks leave open: "judge" or "agent"."""
        return "judge" if self._judge is not None else "agent"

    async def verify(self, spec: RunSpec, result: ExecutionResult) -> Verdicts:
        # The harness decides what to verify deterministically: user-declared checks
        # PLUS whatever the planner extracts from the objective (exact strings). These
        # run in code before the judge — a rule can't gloss over a one-char typo.
        checks = list(spec.checks)
        # An objective that can only be met by DOING something is not satisfied by a
        # verdict that says it was. Deterministic, and it runs before the judge — so a
        # judge cannot narrate an action that never appears in the step log. This is the
        # false pass we observed: "persisted after backgrounding" on a run that never
        # backgrounded the app.
        # Derived action checks stop a PASS that never did the action — the agent's or a
        # judge's. The one run they must not touch: a BLOCKED agent with no judge, which can
        # only end inconclusive; the check would turn "couldn't do it" into an app failure.
        sv = result.self_verdict
        blocked_alone = self._judge is None and sv is not None and not sv.passed and sv.blocked
        if not blocked_alone:
            checks += [Check(kind="action_used", value=a)
                       for a in sorted(required_actions(spec.instructions))]
        if self._planner is not None:
            checks += await self._planner.plan(spec.instructions)
        checks_result = run_checks(checks, result)
        pre = resolve_pre_judge(result, checks_result, spec.checks_exhaustive)
        if pre is not None:
            return Verdicts(resolution=pre, judge=None)
        if self._judge is None:
            return Verdicts(resolution=resolve_without_judge(result), judge=None)

        judged = await self._judge.evaluate(spec.instructions, result)
        self_passed = result.self_verdict.passed if result.self_verdict else None
        resolution = resolve_with_judge(judged.verdict, self_passed)
        return Verdicts(resolution=resolution, judge=judged.verdict, judge_usage=judged.usage)
