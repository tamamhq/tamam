"""Scoring — turn per-run outcomes into the reliability scorecard.

Convention: detecting a defect is the POSITIVE class, i.e. predicting "fail".
  TP = expected fail & got failed   (caught the bug)
  FN = expected fail & got passed   (missed the bug)
  FP = expected pass & got failed   (false alarm)
  TN = expected pass & got passed   (correctly clean)
Errored runs produce no verdict — excluded from precision/recall, tracked as
error_rate. All rates are None when undefined (no division by zero).
"""

from __future__ import annotations

from collections import Counter, defaultdict

from pydantic import BaseModel

from tamam.evals.models import CaseResult


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


class Scores(BaseModel):
    runs: int = 0
    errors: int = 0
    error_rate: float | None = None
    accuracy: float | None = None           # correct verdicts / non-error runs
    catch_rate_recall: float | None = None  # TP/(TP+FN) — bugs caught
    precision: float | None = None          # TP/(TP+FP)
    false_alarm_rate: float | None = None   # FP/(FP+TN) — cried wolf on working features
    disputed_rate: float | None = None
    # reliability across repeats
    cases: int = 0
    flaky_cases: int = 0                     # not unanimous across repeats
    flaky_rate: float | None = None
    unanimous_correct_rate: float | None = None  # cases correct in ALL repeats
    # efficiency (means over non-error runs)
    mean_steps: float | None = None
    mean_duration_s: float | None = None
    mean_tokens: float | None = None


class Report(BaseModel):
    overall: Scores
    by_category: dict[str, Scores] = {}


def _score(results: list[CaseResult]) -> Scores:
    tp = fp = fn = tn = errors = disputed = 0
    steps: list[int] = []
    durs: list[float] = []
    toks: list[int] = []
    total_runs = 0
    flaky = 0
    unanimous_correct = 0

    for cr in results:
        outcomes = []
        for r in cr.runs:
            total_runs += 1
            if r.disputed:
                disputed += 1
            if r.outcome == "error":
                errors += 1
                outcomes.append("error")
                continue
            outcomes.append(r.outcome)
            if r.steps:
                steps.append(r.steps)
            if r.duration_s is not None:
                durs.append(r.duration_s)
            if r.tokens is not None:
                toks.append(r.tokens)
            exp_fail = cr.case.expected == "fail"
            got_fail = r.outcome == "failed"
            if exp_fail and got_fail:
                tp += 1
            elif exp_fail and not got_fail:
                fn += 1
            elif not exp_fail and got_fail:
                fp += 1
            else:
                tn += 1
        # reliability across this case's repeats
        if cr.runs:
            modal = Counter(outcomes).most_common(1)[0][1]
            if modal != len(outcomes):
                flaky += 1
            if all(r.correct and r.outcome != "error" for r in cr.runs):
                unanimous_correct += 1

    non_error = tp + fp + fn + tn
    correct = tp + tn
    n_cases = len(results)
    return Scores(
        runs=total_runs,
        errors=errors,
        error_rate=_rate(errors, total_runs),
        accuracy=_rate(correct, non_error),
        catch_rate_recall=_rate(tp, tp + fn),
        precision=_rate(tp, tp + fp),
        false_alarm_rate=_rate(fp, fp + tn),
        disputed_rate=_rate(disputed, total_runs),
        cases=n_cases,
        flaky_cases=flaky,
        flaky_rate=_rate(flaky, n_cases),
        unanimous_correct_rate=_rate(unanimous_correct, n_cases),
        mean_steps=round(sum(steps) / len(steps), 2) if steps else None,
        mean_duration_s=round(sum(durs) / len(durs), 2) if durs else None,
        mean_tokens=round(sum(toks) / len(toks), 1) if toks else None,
    )


def compute_report(results: list[CaseResult]) -> Report:
    by_cat: dict[str, list[CaseResult]] = defaultdict(list)
    for cr in results:
        by_cat[cr.case.category].append(cr)
    return Report(
        overall=_score(results),
        by_category={cat: _score(rs) for cat, rs in sorted(by_cat.items())},
    )


def correct_for(expected: str, outcome: str) -> bool:
    """Did this run's harness outcome match the known-correct verdict?"""
    return (expected == "fail" and outcome == "failed") or (
        expected == "pass" and outcome == "passed"
    )


def oracle_ok(expected: str, require: list[str], forbid: list[str], text: str | None) -> bool | None:
    """Independent ground-truth check on captured final state (None if no oracle)."""
    if not require and not forbid:
        return None
    hay = (text or "").lower()
    return all(s.lower() in hay for s in require) and all(s.lower() not in hay for s in forbid)
