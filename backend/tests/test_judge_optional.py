"""The LLM judge is optional (off by default).

Without it, verdicts come from the deterministic checks and then the agent's own
evidence-gated verdict.
"""

from tamam.executors.base import ExecutionResult, SelfVerdict
from tamam.harness.verdict import resolve_without_judge
from tamam.harness.verification import Verifier
from tamam.models import Outcome
from tests.conftest import FakeJudge


def _result(**kw) -> ExecutionResult:
    return ExecutionResult(**kw)


# ── resolution without a judge ─────────────────────────────────────────────


def test_agent_pass_is_the_verdict():
    r = resolve_without_judge(_result(self_verdict=SelfVerdict(passed=True, reason="all clauses seen")))
    assert r.outcome == Outcome.PASSED and r.reason == "all clauses seen" and not r.disputed


def test_agent_fail_is_the_verdict():
    r = resolve_without_judge(_result(self_verdict=SelfVerdict(passed=False, reason="no Save button")))
    assert r.outcome == Outcome.FAILED


def test_no_agent_verdict_but_a_failed_clause_is_a_fail():
    r = resolve_without_judge(_result(clauses=[
        {"id": "c1", "text": "Select Electronics", "status": "done", "evidence": "selected"},
        {"id": "c2", "text": "Price is $5", "status": "failed", "evidence": "shows $50"}]))
    assert r.outcome == Outcome.FAILED and "$50" in r.reason


def test_no_agent_verdict_and_nothing_failed_is_inconclusive_not_guessed():
    r = resolve_without_judge(_result(clauses=[
        {"id": "c1", "text": "a", "status": "done"}, {"id": "c2", "text": "b", "status": "pending"}]))
    assert r.outcome == Outcome.ERROR and "1/2" in r.reason


# ── the verifier ───────────────────────────────────────────────────────────


class ExplodingJudge:
    async def evaluate(self, instructions, result):
        raise AssertionError("the judge must not be called when it is disabled")


async def test_verifier_without_judge_uses_the_agent_verdict(make_spec):
    v = Verifier(judge=None)
    assert v.source == "agent"
    out = await v.verify(make_spec(), _result(self_verdict=SelfVerdict(passed=True, reason="ok")))
    assert out.resolution.outcome == Outcome.PASSED and out.judge is None


async def test_deterministic_checks_still_run_first_without_a_judge(make_spec):
    """A failed check decides, whatever the agent claims."""
    spec = make_spec(checks=[{"kind": "text_present", "value": "Order Confirmed"}])
    out = await Verifier(judge=None).verify(
        spec, _result(final_text="Cart", self_verdict=SelfVerdict(passed=True, reason="ok")))
    assert out.resolution.outcome == Outcome.FAILED


async def test_required_action_check_still_blocks_a_false_pass_without_a_judge(make_spec):
    """The false pass the judge used to catch — 'survives backgrounding' without ever
    backgrounding — is caught in code."""
    spec = make_spec(instructions="Select Books, send the app to the background and bring "
                                  "it back, then verify Books is still selected")
    out = await Verifier(judge=None).verify(
        spec, _result(self_verdict=SelfVerdict(passed=True, reason="persisted")))
    assert out.resolution.outcome == Outcome.FAILED
    assert "background_app" in out.resolution.reason


async def test_verifier_with_judge_is_unchanged(make_spec):
    v = Verifier(judge=FakeJudge(passed=False, reason="judge says no"))
    assert v.source == "judge"
    out = await v.verify(make_spec(), _result(self_verdict=SelfVerdict(passed=True, reason="ok")))
    assert out.resolution.outcome == Outcome.FAILED and out.resolution.disputed


# ── configuration ──────────────────────────────────────────────────────────


def test_judge_is_off_by_default():
    from tamam.config import Settings
    assert Settings.model_fields["judge_enabled"].default is False


def test_every_run_takes_its_judge_from_the_same_setting(monkeypatch):
    """Dashboard, evals and CLI assemble runs in one place (tamam.assembly), so the judge
    setting means the same everywhere; an injected judge replaces it (tests)."""
    from tamam.assembly import assemble_run, build_judge
    from tamam.config import get_settings
    from tamam.harness.judge import AnthropicJudge
    from tamam.harness.spec import Budgets, RunSpec

    s = get_settings()
    monkeypatch.setattr(s, "judge_enabled", False)
    assert build_judge(s) is None
    monkeypatch.setattr(s, "judge_enabled", True)
    assert isinstance(build_judge(s), AnthropicJudge)
    spec = RunSpec(instructions="x", execution_prompt="x", checks=[],
                   checks_exhaustive=False, budgets=Budgets(5, 60, 30))
    injected = FakeJudge()
    run = assemble_run(spec, object(), None, judge=injected)
    assert run._verifier._judge is injected


async def test_run_records_who_decided_the_verdict(make_run, run_shell, monkeypatch):
    """The UI shows whether the agent or the judge decided — frozen in the spec snapshot."""
    from tamam.config import get_settings
    from tests.conftest import FakeExecutor
    monkeypatch.setattr(get_settings(), "judge_enabled", False)
    _, _, run_id = await make_run()
    run = await run_shell(run_id, FakeExecutor(
        result=ExecutionResult(self_verdict=SelfVerdict(passed=True, reason="ok"))))
    assert run.spec_snapshot["verdict_source"] == "agent"
    assert run.outcome == Outcome.PASSED and run.judge_verdict is None


def test_the_judge_sees_the_same_evidence_keys_as_before_the_diagnostics_split():
    """The judge's prompt is built from these keys: device errors and crashes reach it exactly
    as they did when they were stored as console_errors / failed_requests."""
    import json

    from tamam.executors.base import Diagnostics
    from tamam.harness.judge import build_evidence
    ev = json.loads(build_evidence(ExecutionResult(
        final_text="Total: $35", diagnostics=Diagnostics(errors=["NSError"], crashes=["SIGABRT"]))))
    assert ev["final_page_text"] == "Total: $35" and ev["final_url"] is None
    assert ev["console_errors"] == ["NSError"] and ev["failed_requests"] == [{"crash": "SIGABRT"}]
