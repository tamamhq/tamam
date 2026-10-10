"""Render + persist the eval scorecard.

The scorecard is the "best in market" artifact: catch-rate, false-alarm, and
flakiness up front, then per-category. Each run is persisted (with prompts_version)
so the regression gate and trend view have history.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from tamam.evals.metrics import Report, Scores, compute_report
from tamam.evals.models import CaseResult


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def _num(v: float | None) -> str:
    return "—" if v is None else f"{v:g}"


def _line(name: str, s: Scores) -> str:
    return (
        f"{name:<16} acc {_pct(s.accuracy):>7} | catch {_pct(s.catch_rate_recall):>7} | "
        f"false-alarm {_pct(s.false_alarm_rate):>7} | flaky {_pct(s.flaky_rate):>7} | "
        f"disputed {_pct(s.disputed_rate):>7} | steps {_num(s.mean_steps):>4} | runs {s.runs}"
    )


def render_scorecard(report: Report) -> str:
    o = report.overall
    lines = [
        "═" * 96,
        "EVAL SCORECARD",
        "═" * 96,
        _line("OVERALL", o),
        f"{'':16} precision {_pct(o.precision)} | unanimous-correct {_pct(o.unanimous_correct_rate)} | "
        f"errors {_pct(o.error_rate)} | mean tokens {_num(o.mean_tokens)} | mean {_num(o.mean_duration_s)}s",
        "-" * 96,
        "by category:",
        *[_line(f"  {cat}", s) for cat, s in report.by_category.items()],
        "═" * 96,
    ]
    return "\n".join(lines)


def save_report(results: list[CaseResult], out_dir: str | Path) -> Path:
    from tamam.harness.instructions import PROMPTS_VERSION

    report = compute_report(results)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = out / f"eval-{stamp}.json"
    path.write_text(json.dumps({
        "timestamp": stamp,
        "prompts_version": PROMPTS_VERSION,
        "report": report.model_dump(),
        "cases": [
            {
                "id": cr.case.id,
                "category": cr.case.category,
                "difficulty": cr.case.difficulty,
                "expected": cr.case.expected,
                "runs": [r.model_dump() for r in cr.runs],
            }
            for cr in results
        ],
    }, indent=2, default=str))
    return path
