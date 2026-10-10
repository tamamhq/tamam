"""Stability gate: is replay of an UNCHANGED app stable?

Each scenario is recorded once by the AI agent on the correct build and compiled; the same
script is then replayed REPEATS times on the same, unchanged build. Kill criteria
(docs/guide/test-graduation.md):
  • replay pass rate < 95% over the unchanged runs (45 by default: 9 scenarios × 5);
  • any "failed" verdict on the correct build — a false app bug;
  • median replay time > 40% of the AI run's time.

A miss is allowed (it costs speed, not correctness: the AI takes over), but counts against
the pass rate; every miss reason is printed so flakiness can be traced.

  TAMAM_FIXTURE_CORRECT_APP=... uv run python scripts/stability_eval.py [--repeats 5] [did ...]
"""

import argparse
import asyncio
import json
import os
import statistics
import time

from tamam.evals.graduation import score_run
from tamam.evals.runner import execute_case
from tamam.evals.scenarios import ios_cases
from tamam.models import Outcome
from tamam.observability import init_observability
from replay_eval import replay  # noqa: E402  (same directory)

# A spread: code checks, a row-bound price, a switch, a scroll, an app dialog, and the
# AI-look-only pages (the most likely to flake).
DEFAULT = ["menu-orders", "cart-total", "list-price", "toggle-switch", "offscreen-category",
           "error-alert", "slow-reviews", "half-loaded-help", "wrong-page"]
MIN_PASS_RATE, MAX_TIME_RATIO = 0.95, 0.40


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("dids", nargs="*")
    args = ap.parse_args()
    init_observability("stability-eval")
    only = set(args.dids or DEFAULT)
    correct = os.environ["TAMAM_FIXTURE_CORRECT_APP"]
    cases = [c for c in ios_cases(correct_app=correct, buggy_app=None, include_real=False)
             if c.id.endswith("-ok") and c.id[:-3] in only]
    verdicts, ratios, false_bugs, misses, rows = [], [], [], [], []
    for case in cases:
        did = case.id[:-3]
        t0 = time.monotonic()
        res = await execute_case(case)
        ai_secs = time.monotonic() - t0
        ev = res.evidence
        recs = ((ev.usage_extra or {}).get("step_trace") or []) if ev else []
        d, _ = score_run(did, res.outcome == Outcome.PASSED, recs, (ev.clauses if ev else []) or [],
                         case.objective, run_error=ev.error if ev else None)
        if not d.ready:
            print(f"--  {did:20} not graduated: {d.reasons[:1]}", flush=True)
            continue
        got = []
        for _ in range(args.repeats):
            out = await replay(d.script, case.objective, correct, case.target.device)
            got.append(out["verdict"])
            if out["verdict"] == "passed":
                ratios.append(out["secs"] / ai_secs)
            elif out["verdict"] == "failed":
                false_bugs.append((did, out["reason"]))
            else:
                misses.append((did, out["reason"]))
        verdicts += got
        ok = got.count("passed")
        rows.append({"did": did, "verdicts": got, "ai_secs": round(ai_secs, 1)})
        print(f"{'OK ' if ok == len(got) else 'FLK'} {did:20} {ok}/{len(got)} passed {got} (AI {ai_secs:.0f}s)",
              flush=True)
    n = len(verdicts)
    rate = verdicts.count("passed") / n if n else 0.0
    median = statistics.median(ratios) if ratios else None
    for did, why in misses:
        print(f"    miss  {did}: {why[:160]}")
    for did, why in false_bugs:
        print(f"    FALSE APP BUG  {did}: {why[:160]}")
    kills = []
    if rate < MIN_PASS_RATE:
        kills.append(f"pass rate {rate:.0%} < {MIN_PASS_RATE:.0%}")
    if false_bugs:
        kills.append(f"{len(false_bugs)} false app bug(s) on the correct build")
    if median is not None and median > MAX_TIME_RATIO:
        kills.append(f"median replay time {median:.0%} of AI > {MAX_TIME_RATIO:.0%}")
    print(f"\nSTABILITY {verdicts.count('passed')}/{n} replays passed ({rate:.0%}) | false app bugs: "
          f"{len(false_bugs)} | median replay time: {median:.0%} of AI" if median is not None else
          f"\nSTABILITY {verdicts.count('passed')}/{n} replays passed ({rate:.0%}) | false app bugs: {len(false_bugs)}")
    print("STABILITY GATE: " + ("PASS" if not kills and n else "FAIL — " + "; ".join(kills or ["no replays"])))
    with open("artifacts/evals/stability-last.json", "w") as fh:
        json.dump({"rows": rows, "rate": rate, "median_ratio": median, "misses": misses,
                   "false_bugs": false_bugs}, fh, indent=1)


if __name__ == "__main__":
    asyncio.run(main())
