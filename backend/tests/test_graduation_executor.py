"""ScriptExecutor: replay first, a full AI run as the safety net; probation always confirms."""

import tamam.graduation.executor as gx
from tamam.executors.base import ExecutionResult, ExecutionSpec, SelfVerdict
from tamam.graduation.replay import ReplayOutcome


class FakeAI:
    def __init__(self, passed=True):
        self.calls = 0
        self.passed = passed

    async def execute(self, spec, on_event, heartbeat=None):
        self.calls += 1
        return ExecutionResult(self_verdict=SelfVerdict(passed=self.passed, reason="ai"))


class FakeDevice:
    """An open device replay left for a hand-off; counts stop / release."""
    def __init__(self):
        self.session, self.reconnects_used = object(), 0
        self.stopped = self.released = 0

    async def stop(self):
        self.stopped += 1

    async def release(self):
        self.released += 1


class FakePlatform:
    def __init__(self, handoff=None):
        self._handoff = handoff     # builds the attached AI: handoff(device=…, **kw)

    def attach(self, services):
        pass

    def agent(self, device=None, **kw):
        return self._handoff(device=device, **kw)


def spec(status="active"):
    return ExecutionSpec(instructions="x", script={"test_text_hash": "h", "steps": [], "checks": [], "coverage": []}, script_status=status, script_id="sid")


async def _run(monkeypatch, verdict, status="active", ai_passed=True):
    async def fake_replay(self, spec, script, looker, on_event, heartbeat, keep_on_miss=False):
        return ReplayOutcome(verdict, f"replay {verdict}"), [], "final text", None, "video.mp4", None
    monkeypatch.setattr(gx.ScriptExecutor, "_replay", fake_replay)
    ai = FakeAI(ai_passed)
    async def noop(e): pass
    res = await gx.ScriptExecutor(FakePlatform(), ai=ai, looker=object()).execute(spec(status), noop)
    return res, ai


async def test_active_replay_pass_is_a_fast_pass_without_ai(monkeypatch):
    res, ai = await _run(monkeypatch, "passed")
    assert ai.calls == 0 and res.self_verdict.passed
    assert res.usage_extra["graduation"]["mode"] == "fast"


async def test_active_replay_failure_on_its_screen_is_a_fail_without_ai(monkeypatch):
    res, ai = await _run(monkeypatch, "failed")
    assert ai.calls == 0 and res.self_verdict.passed is False


async def test_a_miss_falls_back_to_a_full_ai_run(monkeypatch):
    res, ai = await _run(monkeypatch, "miss")
    assert ai.calls == 1 and res.usage_extra["graduation"]["mode"] == "fallback"


async def test_probation_always_confirms_with_ai_and_records_agreement(monkeypatch):
    res, ai = await _run(monkeypatch, "passed", status="probation", ai_passed=True)
    g = res.usage_extra["graduation"]
    assert ai.calls == 1 and g["mode"] == "probation" and g["ai_passed"] is True
    res, _ = await _run(monkeypatch, "passed", status="probation", ai_passed=False)
    assert res.usage_extra["graduation"]["ai_passed"] is False and res.self_verdict.passed is False


async def test_an_unreadable_or_newer_script_runs_with_ai(monkeypatch):
    ai = FakeAI()
    async def noop(e): pass
    s = spec()
    s.script = {"schema": 99, "test_text_hash": "h", "steps": [], "checks": [], "coverage": []}
    res = await gx.ScriptExecutor(FakePlatform(), ai=ai, looker=object()).execute(s, noop)
    assert ai.calls == 1 and res.usage_extra["graduation"]["mode"] == "fallback"


async def test_a_guardrail_during_replay_ends_the_run_without_a_doomed_fallback(monkeypatch):
    async def stopped(self, spec, script, looker, on_event, heartbeat, keep_on_miss=False):
        out = ReplayOutcome("error", "Run stopped: step limit")
        out.cancelled = True
        return out, [], "", None, None, None
    monkeypatch.setattr(gx.ScriptExecutor, "_replay", stopped)
    ai = FakeAI()
    async def noop(e): pass
    res = await gx.ScriptExecutor(FakePlatform(), ai=ai, looker=object()).execute(spec(), noop)
    assert ai.calls == 0 and res.error


async def test_the_ai_fallback_gets_the_normal_step_budget(monkeypatch):
    async def miss(self, spec, script, looker, on_event, heartbeat, keep_on_miss=False):
        return ReplayOutcome("miss", "x"), [], "", None, None, None
    monkeypatch.setattr(gx.ScriptExecutor, "_replay", miss)
    seen = {}

    class AI(FakeAI):
        async def execute(self, spec, on_event, heartbeat=None):
            seen["max_steps"] = spec.max_steps
            return await super().execute(spec, on_event, heartbeat)
    s = spec()
    s.max_steps = 27
    async def noop(e): pass
    await gx.ScriptExecutor(FakePlatform(), ai=AI(), looker=object()).execute(s, noop)
    assert seen["max_steps"] == 25          # 27 minus the replay's (0 steps + 2)



# ── hand-off: an active script's miss continues on the SAME device session ──────────────

def _script_with_coverage():
    from tamam.graduation.script import Check, ClauseCoverage, CompiledScript, Locator, Step, Target
    t = Target(type="Button", label="Cart", locators=[Locator(kind="type_label", value="Button|Cart")])
    return CompiledScript(
        test_text_hash="h",
        steps=[Step(id="s1", say='Tap "Cart"', do="tap", target=t, source_step=1),
               Step(id="s2", say='Tap "Pay"', do="tap", target=t, source_step=2)],
        checks=[Check(id="k1", clause_id="c1", clause="verify 'Cart' shows", after="s1",
                      kind="visible", text="Cart", on_screen=["Cart"])],
        coverage=[ClauseCoverage(clause_id="c1", clause="verify 'Cart' shows", kind="verification",
                                 checked_by_code=True, after="s1"),
                  ClauseCoverage(clause_id="c2", clause="Tap Pay", kind="action",
                                 checked_by_code=False, after="s2")])


async def test_an_active_miss_hands_off_to_the_ai_on_the_same_session(monkeypatch):
    from tamam.graduation.replay import CheckResult
    sc = _script_with_coverage()
    device = FakeDevice()

    async def miss_keeping_session(self, spec, script, looker, on_event, heartbeat, keep_on_miss=False):
        out = ReplayOutcome("miss", '"Tap "Pay"": "Pay" is not on the screen')
        out.steps_done = 1
        out.checks = [CheckResult("k1", "verify 'Cart' shows", "code", True)]
        return out, [], "", None, None, (device if keep_on_miss else None)
    monkeypatch.setattr(gx.ScriptExecutor, "_replay", miss_keeping_session)
    built = {}

    class AttachedAI(FakeAI):
        pass

    def factory(**kw):
        built.update(kw)
        return AttachedAI()
    s = spec("active")
    s.script = sc.model_dump(by_alias=True)
    async def noop(e): pass
    res = await gx.ScriptExecutor(FakePlatform(factory), ai=FakeAI(), looker=object()).execute(s, noop)
    assert built["device"] is device                                      # same session, no restart
    preset = {c.text: c.status for c in built["preset_clauses"]}
    assert preset == {"verify 'Cart' shows": "done", "Tap Pay": "pending"}
    assert "It stopped" in built["seed_history"][0]
    g = res.usage_extra["graduation"]
    assert g["mode"] == "handoff" and device.released == 1
    assert g["proven_by_replay"] == ["verify 'Cart' shows"]   # what REPLAY proved, not the AI


async def test_probation_never_hands_off(monkeypatch):
    seen = {}

    async def miss(self, spec, script, looker, on_event, heartbeat, keep_on_miss=False):
        seen["keep"] = keep_on_miss
        return ReplayOutcome("miss", "x"), [], "", None, None, None
    monkeypatch.setattr(gx.ScriptExecutor, "_replay", miss)
    async def noop(e): pass
    res = await gx.ScriptExecutor(FakePlatform(), ai=FakeAI(), looker=object()).execute(spec("probation"), noop)
    assert seen["keep"] is False and res.usage_extra["graduation"]["mode"] == "probation"


def test_an_action_clause_is_proven_only_by_what_came_after_it():
    from tamam.graduation.executor import proven_clauses
    sc = _script_with_coverage()            # c2 "Tap Pay" is placed after s2
    out = ReplayOutcome("miss", "x")
    out.steps_done = 2                      # s1 and s2 ran, but nothing after s2 held
    assert {c.text: c.status for c in proven_clauses(sc, out)}["Tap Pay"] == "pending"


async def test_a_hand_off_that_cannot_start_releases_the_device_and_falls_back(monkeypatch):
    device = FakeDevice()

    async def miss(self, spec, script, looker, on_event, heartbeat, keep_on_miss=False):
        return ReplayOutcome("miss", "x"), [], "", None, None, device
    monkeypatch.setattr(gx.ScriptExecutor, "_replay", miss)

    def broken_factory(**kw):
        raise RuntimeError("could not build the agent")
    s = spec("active")
    s.script = _script_with_coverage().model_dump(by_alias=True)
    async def noop(e): pass
    ai = FakeAI()
    res = await gx.ScriptExecutor(FakePlatform(broken_factory), ai=ai, looker=object()).execute(s, noop)
    assert device.stopped == 1 and device.released == 1     # no farm session left running
    assert ai.calls == 1 and res.usage_extra["graduation"]["mode"] == "fallback"



def test_action_clauses_are_never_pre_marked_by_replay():
    """Adversarial probe: an action whose tap never happened must not arrive as done, even
    when a verification at an earlier step passed."""
    from tamam.graduation.executor import proven_clauses
    from tamam.graduation.replay import CheckResult
    from tamam.graduation.script import Check, ClauseCoverage, CompiledScript, Locator, Step, Target
    t = Target(type="Button", label="x", locators=[Locator(kind="type_label", value="Button|x")])
    sc = CompiledScript(
        test_text_hash="h",
        steps=[Step(id="s1", say='Tap "Mouse"', do="tap", target=t, source_step=1),
               Step(id="s2", say='Tap "Add to Cart"', do="tap", target=t, source_step=2)],
        checks=[Check(id="k1", clause_id="c1", clause="verify Mouse page shows", after="s1",
                      kind="visible", text="Mouse", on_screen=["Mouse"])],
        coverage=[ClauseCoverage(clause_id="c1", clause="verify Mouse page shows", kind="verification",
                                 checked_by_code=True, after="s1"),
                  ClauseCoverage(clause_id="c2", clause="Add the Mouse to the cart", kind="action",
                                 checked_by_code=False, after="s1")])
    out = ReplayOutcome("miss", '"Tap "Add to Cart"": not on the screen')
    out.steps_done = 1
    out.checks = [CheckResult("k1", "verify Mouse page shows", "code", True)]
    got = {c.text: c.status for c in proven_clauses(sc, out)}
    assert got == {"verify Mouse page shows": "done", "Add the Mouse to the cart": "pending"}


async def test_a_hand_off_keeps_the_replay_steps_as_evidence(monkeypatch):
    from tamam.executors.base import StepEvent
    sc = _script_with_coverage()
    replayed = [StepEvent(step=1, next_goal="Fast replay: Tap Cart", actions=[{"action": "background_app"}])]

    async def miss(self, spec, script, looker, on_event, heartbeat, keep_on_miss=False):
        out = ReplayOutcome("miss", "x")
        out.steps_done = 1
        return out, replayed, "", None, None, FakeDevice()
    monkeypatch.setattr(gx.ScriptExecutor, "_replay", miss)

    class AI(FakeAI):
        async def execute(self, spec, on_event, heartbeat=None):
            r = await super().execute(spec, on_event, heartbeat)
            r.steps = [StepEvent(step=1, actions=[{"action": "tap"}])]
            return r
    s = spec("active")
    s.script = sc.model_dump(by_alias=True)
    async def noop(e): pass
    res = await gx.ScriptExecutor(FakePlatform(lambda **kw: AI()), looker=object()).execute(s, noop)
    assert [st.actions[0]["action"] for st in res.steps] == ["background_app", "tap"]
    assert [st.step for st in res.steps] == [1, 2]


def test_clauses_replay_proved_are_marked_as_checked_in_code():
    """The run page must not label a hand-off's code-proven clause as the agent's evidence."""
    from tamam.harness.clauses import Clause
    c = Clause(id="c1", text="x", status="done", evidence="checked in code during fast replay", how="code")
    assert c.as_dict()["how"] == "code"
    assert "how" not in Clause(id="c2", text="y").as_dict()
