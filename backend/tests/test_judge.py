"""AnthropicJudge resilience: a malformed structured output must not ERROR the run.

Regression for a real run: the Haiku judge emitted repro_steps as text
inside `reasoning`, leaving the JSON field empty; the strict "failed verdict needs
repro" output_validator retried, Haiku malformed it again, and pydantic-ai raised
UnexpectedModelBehavior — crashing verify and erroring the whole run, even though
the verdict's substance (passed/reason) was fine. The judge now falls back to a
lenient run so a real verdict always lands.
"""

import types

from pydantic_ai.exceptions import UnexpectedModelBehavior

import tamam.harness.judge as judge_mod
from tamam.executors.base import ExecutionResult
from tamam.harness.judge import AnthropicJudge
from tamam.harness.verdict import Verdict


class _FakeRun:
    def __init__(self, verdict: Verdict) -> None:
        self.output = verdict
        self.usage = types.SimpleNamespace(total_tokens=42, requests=2)


class _FakeAgent:
    def __init__(self, *, raises: bool = False, verdict: Verdict | None = None) -> None:
        self._raises = raises
        self._verdict = verdict

    async def run(self, parts, usage_limits=None):  # noqa: ANN001
        if self._raises:
            raise UnexpectedModelBehavior("Exceeded maximum output retries (1)")
        return _FakeRun(self._verdict)


async def test_judge_falls_back_when_strict_output_crashes(monkeypatch):
    """Strict path raises (repro-steps retry exhausted) → lenient path produces a
    usable verdict instead of the run erroring."""
    lenient = Verdict(passed=False, reason="login failed", reasoning="form still shown", repro_steps=[])

    def fake_build(model, instructions, strict=True):  # noqa: ANN001
        return _FakeAgent(raises=True) if strict else _FakeAgent(verdict=lenient)

    monkeypatch.setattr(judge_mod, "_build_agent", fake_build)

    res = await AnthropicJudge().evaluate("test login", ExecutionResult())
    assert res.verdict.passed is False
    assert res.verdict.reason == "login failed"  # a real verdict landed, no crash
    assert res.usage["judge_tokens"] == 42


async def test_judge_uses_strict_result_on_success(monkeypatch):
    """When the strict path succeeds, the lenient fallback is never invoked."""
    strict_ok = Verdict(passed=True, reason="login works", repro_steps=[])
    calls = {"strict": 0, "lenient": 0}

    def fake_build(model, instructions, strict=True):  # noqa: ANN001
        calls["strict" if strict else "lenient"] += 1
        return _FakeAgent(verdict=strict_ok) if strict else _FakeAgent(raises=True)

    monkeypatch.setattr(judge_mod, "_build_agent", fake_build)

    res = await AnthropicJudge().evaluate("test login", ExecutionResult())
    assert res.verdict.passed is True
    assert calls["lenient"] == 0


def test_strict_and_lenient_agents_are_distinct():
    """The strict flag is threaded through the cache — strict builds the repro
    validator, lenient does not (distinct cached agents). Uses pydantic-ai's built-in
    'test' model so no ANTHROPIC_API_KEY is needed at construction (CI has none)."""
    strict = judge_mod._build_agent("test", "obj", strict=True)
    lenient = judge_mod._build_agent("test", "obj", strict=False)
    assert strict is not lenient


# ---------- settled final screenshot (transient-state false-FAIL fix) ----------

def test_journey_includes_settled_final_last(tmp_path):
    """The settled end-state screenshot is appended last, labeled — so the judge grades
    a stable frame, not the mid-transition last-step shot."""
    from tamam.executors.base import ExecutionResult, StepEvent
    from tamam.harness.judge import _journey_screenshots

    s1 = tmp_path / "s1.png"; s1.write_bytes(b"AAA")
    s2 = tmp_path / "s2.png"; s2.write_bytes(b"BBB")   # mid-transition last-step frame
    fin = tmp_path / "fin.png"; fin.write_bytes(b"CCC")  # settled end state
    result = ExecutionResult(
        steps=[StepEvent(step=1, screenshot_path=str(s1)),
               StepEvent(step=2, screenshot_path=str(s2))],
        final_screenshot_path=str(fin),
    )
    shots = _journey_screenshots(result)
    assert [lbl for lbl, _ in shots] == ["step 1", "step 2", "final (settled end state)"]
    assert shots[-1][1] == b"CCC"


def test_journey_dedups_settled_against_identical_last_step(tmp_path):
    from tamam.executors.base import ExecutionResult, StepEvent
    from tamam.harness.judge import _journey_screenshots

    s2 = tmp_path / "s2.png"; s2.write_bytes(b"SAME")
    fin = tmp_path / "fin.png"; fin.write_bytes(b"SAME")  # already settled at last step
    result = ExecutionResult(
        steps=[StepEvent(step=2, screenshot_path=str(s2))],
        final_screenshot_path=str(fin),
    )
    shots = _journey_screenshots(result)
    assert len(shots) == 1 and shots[0][0] == "step 2"  # not double-added
