"""Outcome resolution: infra ≠ failed, rules beat LLM opinions, disputes flagged.

resolve_pre_judge returns None to mean "escalate to the judge" — an explicit
absence, not a boolean flag.
"""

from tamam.executors.base import ExecutionResult
from tamam.harness.checks import Check, run_checks
from tamam.harness.verdict import Verdict, resolve_pre_judge, resolve_with_judge
from tamam.models import Outcome


def test_infra_error_is_error_not_failed():
    result = ExecutionResult(error="Browser connection lost during run")
    resolution = resolve_pre_judge(result, run_checks([], result), exhaustive=False)
    assert resolution is not None
    assert resolution.outcome == Outcome.ERROR


def test_failed_check_decides_without_judge():
    result = ExecutionResult(final_url="https://x.example/cart")
    checks = run_checks([Check(kind="url_matches", value="/success")], result)
    resolution = resolve_pre_judge(result, checks, exhaustive=False)
    assert resolution is not None
    assert resolution.outcome == Outcome.FAILED
    assert "url_matches" in resolution.reason


def test_exhaustive_checks_skip_judge_entirely():
    result = ExecutionResult(final_url="https://x.example/success", final_text="Order Confirmed")
    checks = run_checks(
        [Check(kind="url_matches", value="/success"), Check(kind="text_present", value="confirmed")],
        result,
    )
    resolution = resolve_pre_judge(result, checks, exhaustive=True)
    assert resolution is not None
    assert resolution.outcome == Outcome.PASSED
    assert "judge skipped" in resolution.reason


def test_non_exhaustive_passing_checks_escalate():
    result = ExecutionResult(final_url="https://x.example/success")
    checks = run_checks([Check(kind="url_matches", value="/success")], result)
    assert resolve_pre_judge(result, checks, exhaustive=False) is None


def test_no_checks_escalate():
    result = ExecutionResult(final_url="https://x.example")
    assert resolve_pre_judge(result, run_checks([], result), exhaustive=False) is None


def test_judge_agreement_no_dispute():
    verdict = Verdict(passed=True, reason="objective met")
    resolution = resolve_with_judge(verdict, self_verdict_passed=True)
    assert resolution.outcome == Outcome.PASSED
    assert not resolution.disputed


def test_judge_disagreement_flags_dispute_and_judge_wins():
    verdict = Verdict(passed=False, reason="no evidence of success")
    resolution = resolve_with_judge(verdict, self_verdict_passed=True)
    assert resolution.outcome == Outcome.FAILED  # judge is authoritative
    assert resolution.disputed


def test_judge_without_self_verdict_no_dispute():
    verdict = Verdict(passed=False, reason="not verified")
    assert not resolve_with_judge(verdict, self_verdict_passed=None).disputed
