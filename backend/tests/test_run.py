"""HarnessRun core — the spec's verification paths, tested by injecting fakes.

No DB, no network: executor, judge, and event sink are all fakes. This is where
the harness's contract lives.
"""

from tamam.executors.base import ExecutionResult, SelfVerdict, StepEvent
from tamam.models import Lifecycle, Outcome
from tests.conftest import FakeEventSink, FakeExecutor, FakeJudge


async def test_deterministic_path_zero_judge(build_run, make_spec):
    """Exhaustive checks decide; the judge is never awaited."""

    class ExplodingJudge:
        async def evaluate(self, instructions, result):
            raise AssertionError("judge must not be called on the deterministic path")

    run = build_run(
        spec=make_spec(
            checks=[
                {"kind": "url_matches", "value": "/success"},
                {"kind": "text_present", "value": "order confirmed"},
            ],
            checks_exhaustive=True,
        ),
        judge=ExplodingJudge(),
        executor=FakeExecutor(
            result=ExecutionResult(
                final_url="https://x.example/checkout/success",
                final_text="Order Confirmed — thank you!",
                self_verdict=SelfVerdict(passed=True, reason="looks done"),
            ),
        ),
    )
    result = await run.execute()
    assert result.outcome == Outcome.PASSED
    assert result.judge_verdict is None
    assert "judge skipped" in result.reason


async def test_rule_beats_lying_self_verdict(build_run, make_spec):
    """A failed check decides FAILED even if the self-verdict claims success."""
    run = build_run(
        spec=make_spec(checks=[{"kind": "url_matches", "value": "/success"}]),
        judge=FakeJudge(passed=True),  # would pass — but must not be consulted
        executor=FakeExecutor(
            result=ExecutionResult(
                final_url="https://x.example/cart",
                self_verdict=SelfVerdict(passed=True, reason="I definitely did it"),
            ),
        ),
    )
    result = await run.execute()
    assert result.outcome == Outcome.FAILED
    assert result.judge_verdict is None
    assert "url_matches" in result.reason


async def test_infra_error_is_error(build_run):
    run = build_run(executor=FakeExecutor(result=ExecutionResult(error="Failed to navigate")))
    result = await run.execute()
    assert result.outcome == Outcome.ERROR


async def test_dispute_path(build_run, make_spec):
    """Judge disagrees with self-verdict → disputed, judge wins, usage merged."""
    run = build_run(
        spec=make_spec(),  # no checks → judge path
        judge=FakeJudge(passed=False, reason="no evidence", usage={"judge_tokens": 123}),
        executor=FakeExecutor(
            result=ExecutionResult(
                final_url="https://x.example",
                self_verdict=SelfVerdict(passed=True, reason="all good"),
            ),
        ),
    )
    result = await run.execute()
    assert result.outcome == Outcome.FAILED  # judge authoritative
    assert result.disputed is True
    assert result.judge_verdict is not None
    assert result.self_verdict is not None
    assert result.usage.get("judge_tokens") == 123


async def test_events_emitted_to_sink(build_run, make_spec):
    """The run streams steps + lifecycle phases through the injected sink."""
    sink = FakeEventSink()
    run = build_run(
        spec=make_spec(checks=[{"kind": "url_matches", "value": "example"}], checks_exhaustive=True),
        events=sink,
        executor=FakeExecutor(
            events=[
                StepEvent(step=1, url="https://x.example", next_goal="open"),
                StepEvent(step=2, url="https://x.example/a", next_goal="click"),
            ],
            result=ExecutionResult(final_url="https://x.example/a"),
        ),
    )
    await run.execute()
    assert [e.step for e in sink.steps] == [1, 2]
    phases = [p for p, _ in sink.phases]
    assert Lifecycle.RUNNING in phases and Lifecycle.JUDGING in phases
