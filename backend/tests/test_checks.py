"""Layer-1 rule engine: zero tokens, cited evidence."""

from tamam.executors.base import Diagnostics, ExecutionResult
from tamam.harness.checks import Check, run_checks


def evidence(*, console_errors=(), failed_requests=(), crashes=(), **kw) -> ExecutionResult:
    return ExecutionResult(diagnostics=Diagnostics(errors=list(console_errors), crashes=list(crashes),
                                                   failed_requests=list(failed_requests)), **kw)


def test_url_matches():
    result = evidence(final_url="https://shop.example/checkout/success?id=1")
    checks = [Check(kind="url_matches", value=r"/checkout/success")]
    assert run_checks(checks, result).all_passed


def test_url_matches_fails_with_cited_evidence():
    result = evidence(final_url="https://shop.example/cart")
    outcome = run_checks([Check(kind="url_matches", value=r"/checkout/success")], result)
    assert not outcome.all_passed
    assert "https://shop.example/cart" in outcome.failure_reason()


def test_url_matches_not_applicable_when_no_url_context():
    """On a native/mobile run there is no URL (final_url is None). A url_matches check must
    NOT manufacture a false FAIL (which would short-circuit the judge) — it's not applicable."""
    result = evidence(final_url=None, final_text="Home")
    assert run_checks([Check(kind="url_matches", value=r"/checkout/success")], result).all_passed


def test_text_present_case_insensitive():
    result = evidence(final_text="Order Confirmed! Thank you.")
    assert run_checks([Check(kind="text_present", value="order confirmed")], result).all_passed


def test_text_absent():
    result = evidence(final_text="All good here")
    assert run_checks([Check(kind="text_absent", value="error")], result).all_passed
    result_bad = evidence(final_text="Internal Server Error")
    assert not run_checks([Check(kind="text_absent", value="error")], result_bad).all_passed


def test_no_console_errors():
    ok = evidence(console_errors=[])
    bad = evidence(console_errors=["TypeError: x is undefined"])
    check = [Check(kind="no_console_errors")]
    assert run_checks(check, ok).all_passed
    outcome = run_checks(check, bad)
    assert not outcome.all_passed
    assert "TypeError" in outcome.failure_reason()


def test_no_failed_requests():
    bad = evidence(failed_requests=[{"url": "https://api.example/cart", "status": 500}])
    assert not run_checks([Check(kind="no_failed_requests")], bad).all_passed


def test_crashes_have_their_own_check_and_still_fail_a_saved_failed_requests_check():
    crashed = evidence(crashes=["SIGABRT Shoply"])
    assert not run_checks([Check(kind="no_crashes")], crashed).all_passed
    assert not run_checks([Check(kind="no_failed_requests")], crashed).all_passed  # saved app tests
    assert run_checks([Check(kind="no_crashes")], evidence()).all_passed
