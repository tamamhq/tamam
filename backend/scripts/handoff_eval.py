"""Hand-off eval: an ACTIVE script that misses must let the AI finish on the SAME device
session — passing a good build, never passing a buggy one.

For each scenario: AI run on the correct build → compile → break the script on purpose (the
first tap's target renamed to something not on screen, as if the app renamed a button) →
run it through ScriptExecutor as ACTIVE on the correct build (must hand off and PASS) and on
the buggy build (must NOT pass).

  TAMAM_FIXTURE_CORRECT_APP=... TAMAM_FIXTURE_BUGGY_APP=... uv run python scripts/handoff_eval.py [did ...]
"""

import asyncio
import os
import sys
import time

from tamam.evals.runner import execute_case
from tamam.evals.scenarios import FIXTURE_BUNDLE, ios_cases
from tamam.executors.base import AppTarget, ExecutionSpec
from tamam.executors.registry import get_executor
from tamam.graduation.compiler import compile_run
from tamam.graduation.script import Locator
from tamam.models import Outcome
from tamam.observability import init_observability

DEFAULT = ["cart-total", "checkout", "menu-orders"]


def broken(script):
    """Rename the LAST tap's target, so replay proves the earlier steps (and their clauses)
    before it misses — the hand-off then starts mid-test with a pre-filled checklist."""
    s = script.model_copy(deep=True)
    taps = [st for st in s.steps if st.do == "tap" and st.target is not None]
    if not taps:
        return None, ""          # nothing to break (a scenario that only reads the first screen)
    tap = taps[-1]
    tap.target.label = tap.target.label + " (renamed)"
    tap.target.locators = [Locator(kind="type_label", value=f"{tap.target.type}|{tap.target.label}")]
    return s, tap.say


async def run_active(script, objective, app, device):
    spec = ExecutionSpec(instructions=objective, max_steps=30, target=AppTarget(
                         build_ref=app, bundle_id=FIXTURE_BUNDLE, device=device),
                         script=script.model_dump(mode="json", by_alias=True),
                         script_status="active", script_id="eval")
    events = []

    async def on_event(e):  # noqa: ANN001
        events.append(e)
    t0 = time.monotonic()
    res = await get_executor("script").execute(spec, on_event)
    g = (res.usage_extra or {}).get("graduation") or {}
    return {"mode": g.get("mode"), "passed": bool(res.self_verdict and res.self_verdict.passed),
            "error": res.error, "secs": round(time.monotonic() - t0, 1),
            "replay": (g.get("replay") or {}).get("reason", "")[:120],
            "proven": g.get("proven_by_replay"), "steps": len(res.steps)}


async def main() -> None:
    init_observability("handoff-eval")
    only = set(sys.argv[1:]) or set(DEFAULT)
    correct, buggy = os.environ["TAMAM_FIXTURE_CORRECT_APP"], os.environ["TAMAM_FIXTURE_BUGGY_APP"]
    cases = [c for c in ios_cases(correct_app=correct, buggy_app=None, include_real=False)
             if c.id.endswith("-ok") and c.id[:-3] in only]
    bad = checked = 0
    for case in cases:
        did = case.id[:-3]
        t0 = time.monotonic()
        res = await execute_case(case)
        ai_secs = round(time.monotonic() - t0, 1)
        ev = res.evidence
        d = compile_run(((ev.usage_extra or {}).get("step_trace") or []) if ev else [],
                        (ev.clauses if ev else []) or [], case.objective,
                        passed=res.outcome == Outcome.PASSED,
                        allow_risky=True)  # Shoply is a fixture: repeating "Pay" is harmless
        if not d.ready:
            print(f"--   {did:14} not graduated: {d.reasons[:1]}", flush=True)
            continue
        script, broke = broken(d.script)
        if script is None:
            print(f"--   {did:14} no tap to break (reads the first screen only)", flush=True)
            continue
        ok_run = await run_active(script, case.objective, correct, case.target.device)
        bug_run = await run_active(script, case.objective, buggy, case.target.device)
        good = (ok_run["mode"] == "handoff" and ok_run["passed"] and not bug_run["passed"])
        bad += not good
        checked += 1
        print(f"{'OK ' if good else 'BAD'}  {did:14} broke {broke!r} | correct: {ok_run['mode']} "
              f"passed={ok_run['passed']} {ok_run['secs']}s (plain AI {ai_secs}s) proven={ok_run['proven']} "
              f"| buggy: {bug_run['mode']} passed={bug_run['passed']}", flush=True)
        print(f"      replay stopped: {ok_run['replay']}", flush=True)
    if not checked:
        bad += 1                     # an eval that checked nothing proves nothing
        print("--   no scenario was checked (unknown ids, or none graduated)", flush=True)
    print(f"\nHANDOFF {'all OK' if not bad else f'{bad} BAD'} ({checked} checked)")


if __name__ == "__main__":
    asyncio.run(main())
