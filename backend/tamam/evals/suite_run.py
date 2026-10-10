"""Run a list of cases once each, keep every run's step trace, print a scorecard.

Shared by the benchmark and breadth scripts: a failure's root cause (perception, reasoning,
harness, unsupported surface) is read from the trace, not from the verdict.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from tamam.evals import runner as eval_runner
from tamam.evals.metrics import compute_report
from tamam.evals.models import EvalCase
from tamam.evals.report import render_scorecard, save_report
from tamam.evals.runner import run_suite


async def run_traced(cases: list[EvalCase], out_dir: str) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    lines = out / f"cases-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    print(f"Running {len(cases)} cases once each → {lines}\n")
    traces = out / "traces"
    traces.mkdir(exist_ok=True)
    to_outcome = eval_runner._to_run_outcome

    def _traced(case, result):  # noqa: ANN001, ANN202
        ev = getattr(result, "evidence", None)
        if ev is not None:
            (traces / f"{case.id}.json").write_text(json.dumps({
                "objective": case.objective,
                "reason": getattr(result, "reason", ""),
                "self_verdict": getattr(result, "self_verdict", None),
                "final_text": ev.final_text,
                "final_screenshot": ev.final_screenshot_path,
                "error": ev.error,
                "clauses": ev.clauses,
                "steps": [s.model_dump(include={"step", "memory", "next_goal", "actions",
                                                "screenshot_path", "duration_s"})
                          for s in ev.steps],
            }, indent=1, default=str))
        return to_outcome(case, result)

    def progress(cr):  # noqa: ANN001
        r = cr.runs[0]
        print(f"  {cr.case.id:<26} expect={cr.case.expected:<4} got={r.outcome:<6} "
              f"{'OK ' if r.correct else 'BAD'} steps={r.steps} "
              f"s={round(r.duration_s or 0)} tok={r.tokens} | {r.reason[:120]}", flush=True)
        with lines.open("a") as f:
            f.write(json.dumps({"id": cr.case.id, "category": cr.case.category,
                                "expected": cr.case.expected, **r.model_dump()}) + "\n")

    eval_runner._to_run_outcome = _traced
    try:
        results = await run_suite(cases, repeats=1, on_case=progress)
    finally:
        eval_runner._to_run_outcome = to_outcome
    print("\n" + render_scorecard(compute_report(results)))
    print(f"\nsaved → {save_report(results, str(out))}")
