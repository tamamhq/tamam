"""Eval suite — the machine itself must be reliable. Metric math is proven against
known confusion counts; the runner is proven to drive the REAL harness path (checks
→ judge → resolution) with injected fakes — no device, no Appium, no API key."""

from tamam.evals.metrics import compute_report, correct_for, oracle_ok
from tamam.evals.models import CaseResult, EvalCase, RunOutcome, Target
from tamam.evals.runner import run_case


def _run(expected: str, outcome: str, **kw) -> RunOutcome:
    return RunOutcome(outcome=outcome, correct=correct_for(expected, outcome), **kw)


def _case(cid: str, expected: str, runs: list[RunOutcome], category: str = "planted-bug") -> CaseResult:
    case = EvalCase(id=cid, category=category, objective="x", expected=expected, target=Target())
    return CaseResult(case=case, runs=runs)


def test_metrics_confusion_and_reliability():
    # fail=positive. Build known TP=4, FN=1, FP=1, TN=3 across 4 cases.
    results = [
        _case("A", "fail", [_run("fail", "failed")] * 3),                 # TP x3, unanimous-correct
        _case("B", "fail", [_run("fail", "passed"), _run("fail", "failed")]),  # FN + TP, flaky
        _case("C", "pass", [_run("pass", "passed")] * 2),                 # TN x2, unanimous-correct
        _case("D", "pass", [_run("pass", "failed"), _run("pass", "passed")]),  # FP + TN, flaky
    ]
    o = compute_report(results).overall
    assert o.catch_rate_recall == 0.8      # 4/(4+1)
    assert o.precision == 0.8              # 4/(4+1)
    assert o.false_alarm_rate == 0.25      # 1/(1+3)
    assert o.accuracy == round(7 / 9, 4)   # (4+3)/9
    assert o.flaky_cases == 2 and o.flaky_rate == 0.5
    assert o.unanimous_correct_rate == 0.5  # A and C
    assert o.errors == 0


def test_metrics_counts_errors_separately():
    results = [_case("E", "pass", [_run("pass", "error"), _run("pass", "passed")])]
    o = compute_report(results).overall
    assert o.errors == 1
    assert o.error_rate == 0.5
    assert o.accuracy == 1.0  # the one non-error run was correct


def test_oracle_require_and_forbid():
    assert oracle_ok("pass", ["General"], [], "Settings General Wi-Fi") is True
    assert oracle_ok("fail", [], ["XYZZY"], "Settings General Wi-Fi") is True   # absent → ok
    assert oracle_ok("pass", ["General"], [], "nothing here") is False
    assert oracle_ok("pass", [], [], "anything") is None                        # no oracle


# ── runner over the real harness path, with fakes ──

class FakeExec:
    executor_id = "fake"

    def __init__(self, self_pass: bool, text: str):
        self._p, self._t = self_pass, text

    async def execute(self, spec, on_event, heartbeat=None):
        from tamam.executors.base import ExecutionResult, SelfVerdict, StepEvent, Usage

        await on_event(StepEvent(step=1, next_goal="look"))
        return ExecutionResult(
            steps=[StepEvent(step=1)], final_text=self._t,
            self_verdict=SelfVerdict(passed=self._p, reason="fx"), usage=Usage(total_tokens=42),
        )


class FakeJudge:
    def __init__(self, passed: bool):
        self._p = passed

    async def evaluate(self, instructions, result):
        from tamam.harness.spec import JudgeResult
        from tamam.harness.verdict import Verdict

        return JudgeResult(verdict=Verdict(passed=self._p, reason="fj"), usage={"judge_tokens": 5})


def _ecase(expected: str) -> EvalCase:
    return EvalCase(id="c", category="planted-bug", objective="verify it",
                    expected=expected, target=Target(executor_id="fake"),
                    require_text=["Count: 1"])


async def test_runner_pass_case_through_harness():
    cr = await run_case(_ecase("pass"), repeats=2,
                        executor=FakeExec(self_pass=True, text="Count: 1"),
                        judge=FakeJudge(passed=True))
    assert len(cr.runs) == 2
    assert all(r.outcome == "passed" and r.correct for r in cr.runs)
    assert all(r.oracle_ok is True for r in cr.runs)          # final text had "Count: 1"
    assert all(r.tokens == 42 for r in cr.runs)


async def test_evals_run_with_the_same_guardrails_as_the_dashboard(monkeypatch):
    """Evals must measure the harness users run: the stuck/repeat guardrails included."""
    from tamam.harness import run as harness_run
    from tamam.harness.guardrails import NoProgress, RepeatedAction

    seen = []
    real_init = harness_run.HarnessRun.__init__

    def spy(self, *a, **kw):
        seen.append({type(g) for g in kw["guardrails"]})
        real_init(self, *a, **kw)

    monkeypatch.setattr(harness_run.HarnessRun, "__init__", spy)
    await run_case(_ecase("pass"), repeats=1, executor=FakeExec(self_pass=True, text="Count: 1"),
                   judge=FakeJudge(passed=True))
    assert {NoProgress, RepeatedAction} <= seen[0]


class FakePlanner:
    def __init__(self, checks):
        self._checks = checks

    async def plan(self, objective):
        return self._checks


async def test_autonomous_check_catches_what_judge_misses():
    from tamam.harness.checks import Check

    # Buggy build shows "Shopply"; the judge (LLM) glosses over the typo and PASSES.
    # The autonomous text_present("Shoply") check runs in code and catches it.
    cr = await run_case(
        _ecase("fail"), repeats=1,
        executor=FakeExec(self_pass=True, text="Shopply"),
        judge=FakeJudge(passed=True),
        planner=FakePlanner([Check(kind="text_present", value="Shoply")]),
    )
    r = cr.runs[0]
    assert r.outcome == "failed" and r.correct is True   # caught deterministically
    assert r.judge_pass is None                          # judge never consulted (rule decided)


async def test_runner_catches_bug_and_flags_dispute():
    # buggy build: judge fails it (correct), but executor self-claims pass → disputed
    cr = await run_case(_ecase("fail"), repeats=1,
                        executor=FakeExec(self_pass=True, text="Count: 0"),
                        judge=FakeJudge(passed=False))
    r = cr.runs[0]
    assert r.outcome == "failed" and r.correct is True         # bug caught
    assert r.judge_pass is False and r.self_pass is True
    assert r.disputed is True


async def test_suite_verifies_with_the_same_planner_setting_as_production(monkeypatch):
    """Evals must measure the harness customers get: the auto-planner follows
    TAMAM_AUTO_PLAN (off by default), not an always-on eval-only pipeline."""
    import tamam.evals.runner as runner
    from tamam.config import get_settings

    seen = []

    async def fake_run_case(case, *, repeats, executor, judge, planner):
        seen.append(planner)
        return None

    monkeypatch.setattr(runner, "run_case", fake_run_case)
    monkeypatch.setattr(get_settings(), "auto_plan", False)
    await runner.run_suite([object()], repeats=1)
    assert seen == [None]


def test_every_fixture_scenario_has_a_graduation_expectation():
    # A new scenario without one makes the graduation eval report BAD for the wrong reason.
    from tamam.evals.graduation import EXPECTED
    from tamam.evals.scenarios import SPECS
    assert {s["did"] for s in SPECS} <= set(EXPECTED)


def test_score_run_scores_graduation_and_the_risk_block():
    from tamam.evals.graduation import score_run
    from tests.test_graduation_compiler import CART, cart_run
    recs, clauses = cart_run()
    d, r = score_run("cart-total", True, recs, clauses, CART)
    assert d.ready and r.sensible, r.why
    _, r = score_run("checkout", True, recs, clauses, CART)      # "risky" yet nothing risky tapped
    assert not r.sensible and "risk block wrong" in r.why


def test_a_refused_compile_is_sensible_when_refusal_is_expected():
    from types import SimpleNamespace as NS

    from tamam.evals.graduation import ANY_OF, EXPECTED, judge
    EXPECTED["_t"] = ANY_OF([("visible", "Thanks for 4 stars")], None)
    script = NS(checks=[NS(kind="visible", text="Thanks for 4", expected=None)], steps=[])
    r = judge("_t", True, NS(ready=False, reasons=["2 steps tapped by screen position only."],
                             script=script), "verify the screen says 'Thanks for 4 stars'")
    del EXPECTED["_t"]
    assert r.sensible, r.why
