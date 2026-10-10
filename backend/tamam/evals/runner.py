"""Eval runner — execute cases through the REAL harness path.

Each repeat builds a fresh executor + session and runs the full product pipeline
(executor → deterministic checks → independent judge → outcome resolution), so an
eval measures exactly what a user gets. No DB, no live events (NullEventSink). The
executor/judge are injectable so tests drive the loop with fakes — no device, no
Appium, no API key.
"""

from __future__ import annotations

import logging

from tamam.assembly import FROM_SETTINGS, assemble_run, build_planner
from tamam.evals.metrics import correct_for, oracle_ok
from tamam.evals.models import CaseResult, EvalCase, RunOutcome

logger = logging.getLogger(__name__)


class NullEventSink:
    async def step(self, event) -> None:  # noqa: ANN001
        pass

    async def lifecycle(self, phase, message) -> None:  # noqa: ANN001
        pass


def _outcome_name(result) -> str:  # noqa: ANN001
    name = (getattr(result.outcome, "value", None) or str(result.outcome)).lower()
    if "error" in name:
        return "error"
    if "fail" in name:
        return "failed"
    return "passed"


def _to_run_outcome(case: EvalCase, result) -> RunOutcome:  # noqa: ANN001
    outcome = _outcome_name(result)
    evidence = getattr(result, "evidence", None)
    final_text = getattr(evidence, "final_text", None) if evidence else None
    self_v = getattr(result, "self_verdict", None) or {}
    judge_v = getattr(result, "judge_verdict", None) or {}
    usage = getattr(result, "usage", None) or {}
    timings = getattr(result, "timings", None)
    return RunOutcome(
        outcome=outcome,  # type: ignore[arg-type]
        correct=correct_for(case.expected, outcome),
        disputed=bool(getattr(result, "disputed", False)),
        self_pass=self_v.get("passed") if isinstance(self_v, dict) else None,
        judge_pass=judge_v.get("passed") if isinstance(judge_v, dict) else None,
        oracle_ok=oracle_ok(case.expected, case.require_text, case.forbid_text, final_text),
        steps=len(getattr(evidence, "steps", []) or []) if evidence else 0,
        duration_s=getattr(timings, "execute_s", None) if timings else None,
        tokens=usage.get("total_tokens") if isinstance(usage, dict) else None,
        reason=getattr(result, "reason", "") or "",
        error=getattr(evidence, "error", None) if evidence else None,
    )


async def execute_case(case: EvalCase, *, executor=None, judge=None,  # noqa: ANN001
                       planner=FROM_SETTINGS):  # noqa: ANN001
    """One full harness run of a case, assembled exactly like a dashboard run (agent,
    guardrails, verifier, judge and planner from settings) — the real verdict. An injected
    executor, judge or planner replaces that part (tests)."""
    from tamam.config import get_settings
    from tamam.executors.base import AppTarget, WebTarget
    from tamam.executors.registry import get_executor
    from tamam.harness.spec import Budgets, RunSpec

    settings = get_settings()
    budgets = Budgets.from_settings(settings, max_steps=case.max_steps,
                                    max_wall_clock_s=settings.default_max_wall_clock_s)
    spec = RunSpec(
        instructions=case.objective,
        execution_prompt=case.objective,  # per-platform prompt shaping lives in the brain
        checks=[],                        # agent + judge decide; eval knows expected out-of-band
        checks_exhaustive=False,
        budgets=budgets,
        platform=case.target.platform,
        target=(WebTarget(start_url=case.target.start_url) if case.target.platform == "web" else
                AppTarget(build_ref=case.target.app_build_ref, bundle_id=case.target.app_bundle_id,
                          device=case.target.device)),
    )
    run = assemble_run(spec, executor or get_executor(case.target.executor_id), NullEventSink(),
                       judge=FROM_SETTINGS if judge is None else judge, planner=planner,
                       settings=settings)
    return await run.execute()


async def run_case(case: EvalCase, *, repeats: int = 5, executor=None, judge=None,  # noqa: ANN001
                   planner=FROM_SETTINGS) -> CaseResult:  # noqa: ANN001
    """Run one case `repeats` times (sequential, isolated) and collect outcomes."""
    runs: list[RunOutcome] = []
    for i in range(repeats):
        try:
            result = await execute_case(case, executor=executor, judge=judge, planner=planner)
            runs.append(_to_run_outcome(case, result))
        except Exception as exc:  # a broken run is data, not a crash
            logger.exception("eval run failed: %s #%d", case.id, i)
            runs.append(RunOutcome(outcome="error", correct=False, error=str(exc)))
    return CaseResult(case=case, runs=runs)


async def run_suite(
    cases: list[EvalCase], *, repeats: int = 5, executor=None, judge=None,  # noqa: ANN001
    planner="auto", on_case=None,  # noqa: ANN001
) -> list[CaseResult]:
    # "auto" = exactly what production does (TAMAM_AUTO_PLAN, off by default). Evals that
    # verify with a different pipeline than customers get measure the wrong harness: the
    # always-on planner here asserted an EARLY clause's text against the FINAL screen and
    # false-failed a correct multi-clause run. Built once, so its per-objective cache is shared.
    if planner == "auto":
        from tamam.config import get_settings

        planner = build_planner(get_settings())
    results: list[CaseResult] = []
    for case in cases:
        cr = await run_case(case, repeats=repeats, executor=executor, judge=judge, planner=planner)
        results.append(cr)
        if on_case:
            on_case(cr)
    return results

