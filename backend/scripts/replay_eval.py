"""Mutation eval for test graduation (plan P2): does a compiled script CATCH the planted bugs?

For each Shoply scenario: the AI agent passes it on the CORRECT build → compile → replay the
script on the CORRECT build (must pass) and on the BUGGY build (must NOT pass). A replay pass
on the buggy build is a false pass — the one result that must never happen.

  TAMAM_FIXTURE_CORRECT_APP=... TAMAM_FIXTURE_BUGGY_APP=... uv run python scripts/replay_eval.py [did ...]
"""

import asyncio
import json
import os
import sys
import time

from tamam.config import get_settings
from tamam.evals.graduation import score_run
from tamam.evals.runner import execute_case
from tamam.evals.scenarios import FIXTURE_BUNDLE, ios_cases
from tamam.executors.base import AppTarget, ExecutionSpec
from tamam.executors.ios.replay_device import IOSReplayPlatform
from tamam.graduation.look import ClauseLooker
from tamam.graduation.replay import ReplayRunner
from tamam.models import Outcome
from tamam.observability import init_observability


async def replay(script, objective: str, app: str, device: str) -> dict:
    spec = ExecutionSpec(instructions=objective, target=AppTarget(
        build_ref=app, bundle_id=FIXTURE_BUNDLE, device=device))
    looker = ClauseLooker(get_settings().agent_model, test_text=objective)
    t0 = time.monotonic()
    device_ = await IOSReplayPlatform().open(spec)
    try:
        out = await ReplayRunner(device_.session, script, objective, clause_look=looker).run()
    finally:
        await device_.stop()
        await device_.release()
    return {"verdict": out.verdict, "reason": out.reason, "secs": round(time.monotonic() - t0, 1),
            "ai_looks": looker.calls}


async def main() -> None:
    init_observability("replay-eval")
    only = set(sys.argv[1:])
    correct, buggy = os.environ["TAMAM_FIXTURE_CORRECT_APP"], os.environ["TAMAM_FIXTURE_BUGGY_APP"]
    cases = [c for c in ios_cases(correct_app=correct, buggy_app=None, include_real=False)
             if c.id.endswith("-ok") and (not only or c.id[:-3] in only)]
    rows = []
    for case in cases:
        did = case.id[:-3]
        t0 = time.monotonic()
        res = await execute_case(case)
        ai_secs = round(time.monotonic() - t0, 1)
        ev = res.evidence
        recs = ((ev.usage_extra or {}).get("step_trace") or []) if ev else []
        # One recording serves both evals: is the compiled script sensible (graduation eval),
        # and does it replay right (this one)? Risky actions allowed: Shoply is a fixture.
        d, grad = score_run(did, res.outcome == Outcome.PASSED, recs, (ev.clauses if ev else []) or [],
                            case.objective, run_error=ev.error if ev else None)
        if grad.run_passed:
            print(f"{'GRAD OK ' if grad.sensible else 'GRAD BAD'} {did:22} {grad.why}", flush=True)
        row = {"did": did, "ai_passed": res.outcome == Outcome.PASSED, "ai_secs": ai_secs,
               "ready": d.ready, "reasons": d.reasons[:2], "sensible": grad.sensible}
        if d.ready:
            row["checks"] = [(c.kind, c.text, c.with_text) for c in d.script.checks]
            row["on_correct"] = await replay(d.script, case.objective, correct, case.target.device)
            row["on_buggy"] = await replay(d.script, case.objective, buggy, case.target.device)
            ok = row["on_correct"]["verdict"] == "passed" and row["on_buggy"]["verdict"] != "passed"
            row["ok"] = ok
            flag = "OK " if ok else ("FALSE PASS" if row["on_buggy"]["verdict"] == "passed" else "BAD")
            print(f"{flag:10} {did:22} correct={row['on_correct']['verdict']:7}"
                  f"({row['on_correct']['secs']}s vs AI {ai_secs}s) buggy={row['on_buggy']['verdict']:7}"
                  f" checks={row['checks']}", flush=True)
            for side in ("on_correct", "on_buggy"):
                print(f"           {side}: {row[side]['reason'][:150]}", flush=True)
        else:
            print(f"--         {did:22} not graduated: {d.reasons[:1]}", flush=True)
        rows.append(row)
    graded = [r for r in rows if r.get("ready")]
    false_pass = [r["did"] for r in graded if r["on_buggy"]["verdict"] == "passed"]
    print(f"\nGRADUATED {len(graded)}/{len(rows)} | replay passes correct "
          f"{sum(r['on_correct']['verdict'] == 'passed' for r in graded)}/{len(graded)} | "
          f"FALSE PASSES on buggy: {len(false_pass)} {false_pass}")
    scored = [r for r in rows if r["ai_passed"]]
    print(f"SENSIBLE {sum(r['sensible'] for r in scored)}/{len(scored)} "
          f"(AI runs that passed: {len(scored)}/{len(rows)})")
    with open("artifacts/evals/replay-last.json", "w") as fh:
        json.dump(rows, fh, default=str, indent=1)


if __name__ == "__main__":
    asyncio.run(main())
