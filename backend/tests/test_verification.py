"""Verifier: auto-planned checks must not manufacture false FAILs.

Regression for a real run. Objective: "there is option to login and the
login form has SSO and email options" — a *capability* claim. The AutoPlanner
extracted the concept words into literal checks text_present(login/SSO/email);
no real login screen renders the literal string "SSO" (it shows "Continue with
Apple/Google"), so the deterministic layer FAILed and short-circuited the judge,
overriding a correct agent. The planner is now OFF by default (opt-in).
"""

from tamam.executors.base import ExecutionResult, SelfVerdict
from tamam.harness.checks import Check
from tamam.harness.spec import Budgets, JudgeResult, RunSpec
from tamam.harness.verdict import Outcome, Verdict
from tamam.harness.verification import Verifier

# A realistic login screen with SSO + email options — note it never contains the
# literal tokens "login" or "SSO" (that's the whole point).
LOGIN_SCREEN = (
    "Sign in\nContinue with Apple\nContinue with Google\n"
    "Email address\nPassword\nForgot password?"
)


def _spec(instructions: str, checks=None) -> RunSpec:
    return RunSpec(
        instructions=instructions,
        execution_prompt=instructions,
        checks=checks or [],
        checks_exhaustive=False,
        budgets=Budgets(max_steps=25, max_wall_clock_s=300, stuck_timeout_s=60),
    )


def _login_result() -> ExecutionResult:
    return ExecutionResult(
        final_text=LOGIN_SCREEN,
        self_verdict=SelfVerdict(passed=True, reason="login screen shows SSO + email"),
    )


class _RecordingJudge:
    """Records whether it was consulted; rules pass (the agent was right)."""

    def __init__(self) -> None:
        self.called = False

    async def evaluate(self, instructions: str, result: ExecutionResult) -> JudgeResult:
        self.called = True
        return JudgeResult(verdict=Verdict(passed=True, reason="SSO + email present"), usage={})


class _BrittlePlanner:
    """Reproduces what the live AutoPlanner emitted for this objective."""

    async def plan(self, objective: str) -> list[Check]:
        return [Check(kind="text_present", value=v) for v in ("login", "SSO", "email")]


async def test_autoplanner_manufactures_false_fail():
    """With the planner on, the brittle literal checks FAIL and the judge never runs
    — the bug we are fixing."""
    judge = _RecordingJudge()
    verifier = Verifier(judge=judge, planner=_BrittlePlanner())

    verdicts = await verifier.verify(_spec("login form has SSO and email options"), _login_result())

    assert verdicts.resolution.outcome == Outcome.FAILED  # false negative
    assert judge.called is False                            # judge was short-circuited


async def test_planner_off_lets_the_judge_decide():
    """With the planner off (the new default), no auto-checks are manufactured, so a
    capability objective reaches the judge and the correct PASS stands."""
    judge = _RecordingJudge()
    verifier = Verifier(judge=judge, planner=None)

    verdicts = await verifier.verify(_spec("login form has SSO and email options"), _login_result())

    assert judge.called is True
    assert verdicts.resolution.outcome == Outcome.PASSED


async def test_explicit_user_checks_still_enforced():
    """Turning the planner off must NOT disable user-authored deterministic checks —
    those remain authoritative (a genuine text check still fails a bad screen)."""
    judge = _RecordingJudge()
    verifier = Verifier(judge=judge, planner=None)

    spec = _spec("show the confirmation", checks=[Check(kind="text_present", value="Order Confirmed")])
    verdicts = await verifier.verify(spec, ExecutionResult(final_text="Cart is empty"))

    assert verdicts.resolution.outcome == Outcome.FAILED
    assert judge.called is False  # a real failing check still decides in code


# --- an objective that requires DOING cannot be passed by narration --------------

def _stepped(*actions) -> ExecutionResult:
    from tamam.executors.base import StepEvent
    return ExecutionResult(
        steps=[StepEvent(step=i + 1, actions=[a]) for i, a in enumerate(actions)],
        self_verdict=SelfVerdict(passed=False, reason="filter never selected"),
    )


BACKGROUND_OBJECTIVE = ("apply the filter, send the app to the background and bring it "
                        "back, and verify the filter persists")


async def test_judge_cannot_pass_a_run_that_skipped_a_required_action():
    """Observed live: the agent reported failure, the judge reported 'persisted after
    backgrounding', and the run shipped green — with zero background_app calls."""
    judge = _RecordingJudge()
    verdicts = await Verifier(judge=judge).verify(
        _spec(BACKGROUND_OBJECTIVE), _stepped({"action": "tap", "ref": "e1"}))

    assert verdicts.resolution.outcome == Outcome.FAILED
    assert "background_app" in verdicts.resolution.reason
    assert judge.called is False, "a failed check must short-circuit before the judge"


async def test_the_same_objective_passes_once_the_action_really_happened():
    judge = _RecordingJudge()
    verdicts = await Verifier(judge=judge).verify(
        _spec(BACKGROUND_OBJECTIVE),
        _stepped({"action": "tap", "ref": "e1"}, {"action": "background_app", "seconds": 2}))

    assert verdicts.resolution.outcome == Outcome.PASSED
    assert judge.called is True


async def test_objectives_needing_no_special_action_are_left_alone():
    """The guard must not manufacture checks for ordinary objectives — the failure
    mode that put AutoPlanner behind a flag."""
    judge = _RecordingJudge()
    verdicts = await Verifier(judge=judge).verify(
        _spec("verify the banner background colour is blue"), _stepped({"action": "tap"}))

    assert verdicts.resolution.outcome == Outcome.PASSED
    assert judge.called is True


async def test_a_blocked_agent_is_inconclusive_not_an_app_failure():
    """"I couldn't do it" (a missing gesture, an unreachable control) is not "the app is
    broken" — even when the step it was blocked on is a required action."""
    from tamam.executors.base import SelfVerdict
    result = _stepped({"action": "tap", "ref": "e1"})
    result.self_verdict = SelfVerdict(passed=False, reason="Hold me needs a long-press",
                                      blocked="missing_action")
    verdicts = await Verifier(judge=None).verify(_spec(BACKGROUND_OBJECTIVE), result)
    assert verdicts.resolution.outcome == Outcome.ERROR
    assert verdicts.resolution.reason.startswith("Blocked (missing action)")


async def test_an_app_failure_is_still_failed():
    from tamam.executors.base import SelfVerdict
    result = _stepped({"action": "tap", "ref": "e1"})
    result.self_verdict = SelfVerdict(passed=False, reason="Total shows $25, not $35")
    verdicts = await Verifier(judge=None).verify(_spec("Add two items and verify the total"), result)
    assert verdicts.resolution.outcome == Outcome.FAILED
