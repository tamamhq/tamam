"""Run the breadth suite: open-source apps of other kinds, through OUR harness.

Build each app from its pinned commit (docs/results.md), then e.g.:

  uv run python scripts/breadth_eval.py flutter-forms \
      --app ~/oss-apps/flutter-samples/form_app/build/ios/iphonesimulator/Runner.app
  uv run python scripts/breadth_eval.py wikipedia \
      --app ~/oss-apps/dd-wikipedia/Build/Products/Debug-iphonesimulator/Wikipedia.app

Results land in artifacts/breadth/<app>/ (scorecard JSON, one line per case, traces).
"""

from __future__ import annotations

import argparse
import asyncio

from tamam.evals.breadth import APPS, breadth_cases
from tamam.evals.suite_run import run_traced
from tamam.observability import init_observability


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("name", choices=sorted(APPS))
    ap.add_argument("--app", required=True, help="the simulator .app built from the pinned commit")
    ap.add_argument("--device", default="iPhone 16 Pro")
    ap.add_argument("--case", action="append", default=[], help="only these case ids (repeatable)")
    args = ap.parse_args()

    init_observability(f"breadth-{args.name}")
    cases = breadth_cases(args.name, app=args.app, device=args.device)
    if args.case:
        cases = [c for c in cases if c.id in args.case]
    await run_traced(cases, f"artifacts/breadth/{args.name}")


if __name__ == "__main__":
    asyncio.run(main())
