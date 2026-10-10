"""Graduation eval (plan P1) — standalone. The regression gate gets the same scoring from
replay_eval.py, which records each scenario once for both. Graduation eval: run each Shoply scenario on the correct build with the AI agent,
compile the run, and score whether the script is sensible (see tamam/evals/graduation.py).

  TAMAM_FIXTURE_CORRECT_APP=... uv run python scripts/graduation_eval.py [did ...]
"""

import asyncio
import json
import os
import sys

from tamam.evals.graduation import score_run
from tamam.evals.runner import execute_case
from tamam.evals.scenarios import ios_cases
from tamam.models import Outcome
from tamam.observability import init_observability


async def main() -> None:
    init_observability("graduation-eval")
    only = set(sys.argv[1:])
    cases = [c for c in ios_cases(correct_app=os.environ["TAMAM_FIXTURE_CORRECT_APP"], buggy_app=None,
                                  include_real=False)
             if c.id.endswith("-ok") and (not only or c.id[:-3] in only)]
    results = []
    for case in cases:
        did = case.id[:-3]
        res = await execute_case(case)
        ev = res.evidence
        recs = (ev.usage_extra or {}).get("step_trace") or [] if ev else []
        passed = res.outcome == Outcome.PASSED
        _, r = score_run(did, passed, recs, (ev.clauses if ev else []) or [], case.objective,
                         run_error=ev.error if ev else None)
        results.append(r)
        mark = "OK " if r.sensible else ("-- " if not r.run_passed else "BAD")
        print(f"{mark} {did:22} ready={r.ready!s:5} checks={r.checks} {r.why}", flush=True)
        if r.reasons:
            print(f"      reasons: {r.reasons}", flush=True)
        print(f"      steps: {r.steps}", flush=True)
    scored = [r for r in results if r.run_passed]
    ok = sum(r.sensible for r in scored)
    print(f"\nSENSIBLE {ok}/{len(scored)} (AI runs that passed: {len(scored)}/{len(results)})")
    print(json.dumps([r.__dict__ for r in results], default=str)[:200000],
          file=open("artifacts/evals/graduation-last.json", "w"))


if __name__ == "__main__":
    asyncio.run(main())
