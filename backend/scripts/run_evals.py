"""Run the iOS eval suite and print + persist the scorecard.

Prereqs (same as any iOS run): Xcode + Appium server + a booted simulator, and the
fixture app built in both variants (see fixtures/ios/README.md). Point the suite
at the builds via env:

  TAMAM_FIXTURE_CORRECT_APP=/path/Fixture-correct.app \
  TAMAM_FIXTURE_BUGGY_APP=/path/Fixture-buggy.app \
  uv run python scripts/run_evals.py --repeats 5 --device "iPhone 15"

Omit the fixture paths to run only the real-app (Settings) cases.
"""

from __future__ import annotations

import argparse
import asyncio
import os

from tamam.evals.report import render_scorecard, save_report
from tamam.evals.runner import run_suite
from tamam.evals.scenarios import ios_cases
from tamam.observability import init_observability


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=5, help="runs per case (flakiness)")
    ap.add_argument("--device", default="iPhone 15")
    ap.add_argument("--category", default=None, help="filter to one category")
    ap.add_argument("--case", action="append", default=[],
                    help="only scenarios with this id prefix, e.g. menu-orders (repeatable)")
    ap.add_argument("--no-real", action="store_true", help="skip real-app (Settings) cases")
    ap.add_argument("--out", default="artifacts/evals")
    args = ap.parse_args()

    init_observability("evals")
    cases = ios_cases(
        correct_app=os.environ.get("TAMAM_FIXTURE_CORRECT_APP"),
        buggy_app=os.environ.get("TAMAM_FIXTURE_BUGGY_APP"),
        device=args.device,
        include_real=not args.no_real,
    )
    if args.category:
        cases = [c for c in cases if c.category == args.category]
    if args.case:
        cases = [c for c in cases if any(c.id.startswith(p) for p in args.case)]
    if not cases:
        print("No cases — set TAMAM_FIXTURE_CORRECT_APP / TAMAM_FIXTURE_BUGGY_APP or drop --no-real.")
        return

    print(f"Running {len(cases)} cases × {args.repeats} repeats = {len(cases) * args.repeats} runs\n")

    def progress(cr):
        got = [r.outcome for r in cr.runs]
        ok = sum(r.correct for r in cr.runs)
        print(f"  {cr.case.id:<18} expect={cr.case.expected:<4} → {ok}/{len(cr.runs)} correct  {got}")

    results = await run_suite(cases, repeats=args.repeats, on_case=progress)

    from tamam.evals.metrics import compute_report
    print("\n" + render_scorecard(compute_report(results)))
    path = save_report(results, args.out)
    print(f"\nsaved → {path}")


if __name__ == "__main__":
    asyncio.run(main())
