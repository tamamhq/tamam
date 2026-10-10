"""The clause plan: seeded from the test, shown every turn, enforced at `done`."""

from tamam.executors.base import ExecutionSpec
from tamam.executors.ios.brain import ClauseUpdate, Done, Tap, Turn
from tamam.executors.ios.executor import IOSAgentExecutor
from tamam.harness.clauses import HeuristicPlanner
from tests.test_ios_executor import FakeDeviceDriver, FakePerceiver, NoVision


async def _noop(e): pass


class TurnDecider:
    total_tokens = 0

    def __init__(self, turns):
        self._t = list(turns)
        self.contexts, self.histories = [], []

    async def decide(self, objective, space, history, screenshot):
        self.contexts.append(space.context)
        self.histories.append(list(history))
        return self._t.pop(0)


def _run(turns, objective="Select the Toys category, then save the search", n=10):
    dec = TurnDecider(turns)
    ex = IOSAgentExecutor(driver=FakeDeviceDriver(), perceiver=FakePerceiver(),
                          vision=NoVision(), decider=dec)
    spec = ExecutionSpec(instructions=objective, max_steps=n)
    return ex, dec, spec


def _done(ok=True, updates=()):
    return Turn(clause_updates=list(updates), action=Done(passed=ok, reason="r"))


async def test_heuristic_planner_splits_on_sequencing_words():
    got = await HeuristicPlanner().plan(
        "Open Search and select the Toys category, then save the search. "
        "Background the app and confirm the filter survives")
    assert got == ["Open Search and select the Toys category", "save the search",
                   "Background the app and confirm the filter survives"]


async def test_heuristic_planner_keeps_a_single_clause_objective_whole():
    assert await HeuristicPlanner().plan("Log in") == ["Log in"]


async def test_checklist_is_shown_to_the_agent_every_turn():
    ex, dec, spec = _run([Turn(action=Tap(ref="e1")), _done(False)])
    await ex.execute(spec, on_event=_noop)
    assert all("c1: Select the Toys category" in c and "c2: save the search" in c
               for c in dec.contexts)


async def test_pass_with_an_uncovered_clause_is_refused_then_the_agent_finishes():
    """The dominant real-app failure: stopping before every clause is covered."""
    ex, dec, spec = _run([
        Turn(clause_updates=[ClauseUpdate(id="c1", status="done", step=1, evidence="Toys selected")],
             action=Tap(ref="e1")),
        _done(True),                                    # premature: c2 has no evidence
        _done(True, [ClauseUpdate(id="c2", status="done", step=3, evidence="Saved toast")]),
    ])
    result = await ex.execute(spec, on_event=_noop)
    assert any("c2: save the search" in h and "no observed evidence" in h
               for h in dec.histories[2])
    assert result.self_verdict.passed is True
    assert [c["status"] for c in result.clauses] == ["done", "done"]
    assert result.clauses[1]["evidence"] == "Saved toast"


async def test_the_gate_is_bounded_and_never_blocks_forever():
    ex, dec, spec = _run([_done(True)] * 3)
    result = await ex.execute(spec, on_event=_noop)
    assert result.self_verdict is not None  # accepted on the 3rd try (2 refusals max)


async def test_an_honest_fail_is_never_blocked():
    ex, dec, spec = _run([_done(False)])
    result = await ex.execute(spec, on_event=_noop)
    assert result.self_verdict.passed is False and len(result.steps) == 1


async def test_pass_contradicting_a_failed_clause_is_refused():
    ex, dec, spec = _run([
        _done(True, [ClauseUpdate(id="c1", status="done", step=1, evidence="ok"),
                     ClauseUpdate(id="c2", status="failed", step=1, evidence="no Save button")]),
        _done(False),
    ])
    result = await ex.execute(spec, on_event=_noop)
    assert "marked c2 (save the search) as FAILED" in dec.histories[1][-1]
    assert result.self_verdict.passed is False


async def test_evidence_citing_a_future_step_is_rejected():
    ex, dec, spec = _run([
        Turn(clause_updates=[ClauseUpdate(id="c1", status="done", step=9, evidence="x")],
             action=Tap(ref="e1")),
        _done(False),
    ])
    result = await ex.execute(spec, on_event=_noop)
    assert result.clauses[0]["status"] == "pending"
    assert any("cites step 9, which has not happened" in h for h in dec.histories[1])


async def test_memory_is_carried_into_the_next_turn():
    ex, dec, spec = _run([Turn(memory="list price was $12", action=Tap(ref="e1")), _done(False)])
    result = await ex.execute(spec, on_event=_noop)
    assert "list price was $12" in dec.contexts[1]
    assert result.steps[0].memory == "list price was $12"


async def test_budget_warning_names_the_open_clauses_late_in_the_run():
    ex, dec, spec = _run([Turn(action=Tap(ref="e1"))] * 3 + [_done(False)], n=4)
    await ex.execute(spec, on_event=_noop)
    assert "BUDGET" not in dec.contexts[0]
    assert "BUDGET: 2 steps left and 2 clause(s) still open" in dec.contexts[2]


async def test_bare_action_deciders_are_not_gated():
    """Legacy/scripted deciders cannot report progress, so the ledger stays passive."""
    from tests.test_ios_executor import ScriptedDecider
    ex = IOSAgentExecutor(driver=FakeDeviceDriver(), perceiver=FakePerceiver(), vision=NoVision(),
                          decider=ScriptedDecider([Done(passed=True, reason="ok")]))
    result = await ex.execute(ExecutionSpec(instructions="a then b", max_steps=3), on_event=_noop)
    assert result.self_verdict.passed is True


async def test_a_failed_clause_tells_the_agent_to_stop():
    """Fail fast: one failed clause fails the test, so the agent is told to conclude
    rather than burn steps on the rest of a slow app."""
    ex, dec, spec = _run([
        Turn(clause_updates=[ClauseUpdate(id="c1", status="failed", step=1, evidence="no filter")],
             action=Tap(ref="e1")),
        _done(False),
    ])
    await ex.execute(spec, on_event=_noop)
    assert "STOP: c1 has FAILED" in dec.contexts[1]
    assert "STOP" not in dec.contexts[0]


async def test_history_step_numbers_match_the_harness_so_citations_are_accepted():
    """Hints and harness notes used to be numbered as history lines, so the agent cited a
    line number that ran ahead of the real step and correct evidence was rejected (about half
    the runs on a real app). Action entries now carry their real step number."""
    ex, dec, spec = _run([
        Turn(action=Tap(ref="e1")),
        Turn(action=Tap(ref="e1")),
        Turn(clause_updates=[ClauseUpdate(id="c1", status="done", step=2, evidence="seen")],
             action=Tap(ref="e1")),
        _done(False),
    ])
    result = await ex.execute(spec, on_event=_noop)
    assert result.clauses[0]["status"] == "done"
    assert any(h.startswith("[step 2] tapped") for h in dec.histories[-1])
    assert not any("cites step" in h for h in dec.histories[-1])


async def test_a_preset_checklist_is_used_as_is_and_gates_done():
    from tamam.executors.ios.brain import Done
    from tamam.executors.ios.hooks import StepContext
    from tamam.executors.ios.plan import ClauseLedger
    from tamam.harness.clauses import Clause

    class NoPlan:
        async def plan(self, objective):
            raise AssertionError("a preset checklist must not be re-planned")
    preset = [Clause(id="c1", text="verify 'Cart' shows", status="done", evidence="replay"),
              Clause(id="c2", text="Tap Pay")]
    ledger = ClauseLedger(NoPlan(), preset=preset)
    ctx = StepContext(objective="x", driver=None)
    await ledger.before_observe(ctx)
    refusal = await ledger.on_done(ctx, Done(passed=True, reason="ok"))
    assert "c2" in refusal and "c1" not in refusal.split("—")[1]


# ── a PASS must quote what the page shows, not its title ──────────────

from tamam.executors.core.contracts import ActionSpace, Element, Frame  # noqa: E402


class PagePerceiver:
    """Step 1: the More list. After the tap: the Terms page — blank, or with its content."""

    def __init__(self, body=()):
        self.body, self.calls = list(body), 0

    async def observe(self, driver):
        self.calls += 1
        if self.calls == 1:
            els = [Element(ref="e1", type="StaticText", label="More", frame=Frame(0, 60, 100, 40)),
                   Element(ref="e2", type="Button", label="Terms of Service", frame=Frame(0, 300, 390, 44))]
        else:
            els = [Element(ref="e1", type="Button", label="More", frame=Frame(0, 50, 60, 40)),
                   Element(ref="e2", type="StaticText", label="Terms of Service", frame=Frame(0, 90, 300, 20)),
                   *[Element(ref=f"b{i}", type="StaticText", label=t, frame=Frame(0, 200 + 30 * i, 390, 20))
                     for i, t in enumerate(self.body)],
                   Element(ref="t1", type="Button", label="Shop", frame=Frame(0, 790, 100, 50))]
        return ActionSpace(elements=els, text="", screen_w=390, screen_h=844, source_hash=str(self.calls))


def _terms_run(turns, body=()):
    dec = TurnDecider(turns)
    ex = IOSAgentExecutor(driver=FakeDeviceDriver(), perceiver=PagePerceiver(body), vision=NoVision(),
                          decider=dec, planner=HeuristicPlanner())
    spec = ExecutionSpec(instructions="Tap Terms of Service. Verify the terms are shown", max_steps=10)
    return ex, dec, spec


def _terms_turns(evidence, *more):
    return [Turn(clause_updates=[ClauseUpdate(id="c1", status="done", step=1, evidence="tapped")],
                 action=Tap(ref="e2")),
            _done(True, [ClauseUpdate(id="c2", status="done", step=2, evidence=evidence)]), *more]


async def test_a_pass_resting_on_the_page_title_is_refused():
    ex, dec, spec = _terms_run(_terms_turns("Terms of Service screen opened showing title and back nav",
                                            _done(False)))
    result = await ex.execute(spec, on_event=_noop)
    assert any("does not quote anything the page itself showed" in h for h in dec.histories[2])
    assert result.self_verdict.passed is False      # the agent looked again and failed it


async def test_a_pass_quoting_the_pages_content_is_accepted():
    ex, _, spec = _terms_run(_terms_turns('it shows "By creating an account you agree"'),
                             body=["By creating an account you agree to these terms."])
    result = await ex.execute(spec, on_event=_noop)
    assert result.self_verdict.passed is True


async def test_evidence_naming_text_that_is_not_on_screen_is_refused():
    ex, dec, spec = _terms_run(_terms_turns('section "1. Using Shoply" is listed', _done(False)),
                               body=["Last updated: 1 September 2026"])
    await ex.execute(spec, on_event=_noop)
    assert any("does not quote" in h for h in dec.histories[2])


# ── an exhausted gate never yields a PASS; nothing is silently dropped ─

async def test_insisting_on_a_title_only_pass_ends_inconclusive_not_passed():
    from tamam.harness.verdict import resolve_without_judge
    from tamam.models import Outcome
    title_only = "Terms of Service screen opened showing title and back nav"
    ex, dec, spec = _terms_run(_terms_turns(title_only, _done(True), _done(True), _done(True)))
    result = await ex.execute(spec, on_event=_noop)
    assert result.self_verdict.passed is True                 # the loop still ends (bounded)
    weak = [c for c in result.clauses if c.get("unsupported")]
    assert [c["id"] for c in weak] == ["c2"]
    res = resolve_without_judge(result)
    assert res.outcome == Outcome.ERROR and "no on-screen evidence" in res.reason


async def test_insisting_with_an_uncovered_clause_ends_inconclusive_not_passed():
    from tamam.harness.verdict import resolve_without_judge
    from tamam.models import Outcome
    ex, dec, spec = _run([_done(True)] * 3)
    result = await ex.execute(spec, on_event=_noop)
    assert resolve_without_judge(result).outcome == Outcome.ERROR


async def test_an_evidenced_pass_still_passes():
    from tamam.harness.verdict import resolve_without_judge
    from tamam.models import Outcome
    ex, _, spec = _terms_run(_terms_turns('it shows "By creating an account you agree"'),
                             body=["By creating an account you agree to these terms."])
    assert resolve_without_judge(await ex.execute(spec, on_event=_noop)).outcome == Outcome.PASSED


def test_a_long_test_keeps_every_part_on_the_checklist():
    from tamam.harness.clauses import MAX_CLAUSES, fit_clauses
    parts = [f"step {i}" for i in range(1, MAX_CLAUSES + 4)]
    got = fit_clauses(parts)
    assert len(got) == MAX_CLAUSES
    assert all(p in " ".join(got) for p in parts)            # folded, never dropped
    assert fit_clauses(parts[:3]) == parts[:3]


async def test_an_absence_claim_is_checked_on_the_screen_not_quoted():
    """ gate: "Verify the Wireless Mouse is not shown" — its evidence names what is absent.
    Accept it when the cited screen really lacks it (and shows something); refuse otherwise."""
    from tamam.executors.core.contracts import ActionSpace, Element, Frame
    from tamam.executors.ios.hooks import StepContext
    from tamam.executors.ios.plan import ClauseLedger
    from tamam.harness.clauses import Clause, HeuristicPlanner

    def screen(*labels):
        return ActionSpace(elements=[Element(ref=f"e{i}", type="StaticText", label=t,
                                             frame=Frame(0, 200 + 40 * i, 300, 30))
                                     for i, t in enumerate(labels)], text="", screen_w=390,
                           screen_h=844, source_hash="h")

    async def check(shown):
        led = ClauseLedger(HeuristicPlanner(), preset=[Clause(
            id="c1", text="Verify the Wireless Mouse is not shown", status="done", step=2,
            evidence="Wireless Mouse not in the list")])
        ctx = StepContext(objective="x", driver=None)
        ctx.step_num, ctx.space = 2, screen(*shown)
        await led.after_observe(ctx)
        return led.unsupported()

    assert await check(["Swift Book, $40", "Design Book, $35"]) == []          # really absent
    assert [c.id for c in await check(["Wireless Mouse, $25", "Swift Book, $40"])] == ["c1"]
    assert [c.id for c in await check([])] == ["c1"]                           # blank proves nothing
# ── a dialog that lands after the agent's last look ───────────────────

_ALERT_SRC = ('<XCUIElementTypeApplication name="Shoply" label="Shoply">'
              '<XCUIElementTypeAlert name="Couldn\'t save your changes" label="Couldn\'t save your changes"/>'
              '</XCUIElementTypeApplication>')


async def test_a_dialog_that_appears_after_the_last_look_blocks_the_pass():
    from types import SimpleNamespace as NS

    from tamam.executors.ios.hooks import SettledVerdictGate, StepContext

    async def no_wait(_):
        return None

    class Driver:
        async def accessibility(self):
            return _ALERT_SRC
    gate = SettledVerdictGate(settle_s=1.0, sleep=no_wait)
    ctx = StepContext(objective="Save and verify it is saved", driver=Driver())
    ctx.space = NS(alert="")
    refusal = await gate.on_done(ctx, Done(passed=True, reason="Saved"))
    assert refusal and "Couldn't save your changes" in refusal
    ctx.space = NS(alert="Couldn't save your changes")       # the agent already saw it: no repeat
    assert await gate.on_done(ctx, Done(passed=True, reason="r")) is None
    assert await gate.on_done(ctx, Done(passed=False, reason="r")) is None   # a FAIL needs no check


def test_a_blocked_code_outside_the_list_never_rejects_the_turn():
    """ gate: a strict enum made the model's turn fail validation (output-retry error)."""
    from tamam.executors.ios.brain import Done
    assert Done(passed=False, reason="r", blocked="missing_action").blocked == "missing_action"
    assert Done(passed=False, reason="r", blocked="Missing action").blocked == "missing_action"
    for junk in ("none", "", None, "app_bug"):
        assert Done(passed=False, reason="r", blocked=junk).blocked is None


def test_settled_history_comes_first_and_is_stable_from_step_to_step():
    """the cacheable prefix must be byte-identical between consecutive steps."""
    from tamam.executors.ios.brain import render_step_blocks
    from tamam.executors.core.contracts import ActionSpace
    sp = lambda n: ActionSpace(elements=[], text=f"screen {n}", screen_w=390, screen_h=844,  # noqa: E731
                               source_hash=str(n), step=n, context=f"checklist at {n}")
    h = [f"[step {i}] tapped e{i}" for i in range(1, 6)]
    s5, live5 = render_step_blocks(sp(6), h)
    s6, live6 = render_step_blocks(sp(7), h + ["[step 6] tapped e6"])
    assert s5 and s6[:len(s5)] == s5                      # step 6's blocks reappear unchanged
    assert len(s6) == len(s5) + 1                         # one new settled block per step
    assert "checklist at 6" in live5 and not any("checklist" in b for b in s5)
    assert "[step 5]" in live5 and not any("[step 5]" in b for b in s5)   # newest stay live
    assert render_step_blocks(sp(1), [])[0] == []         # nothing to cache on step 1


async def test_a_quote_only_in_the_pixels_counts_when_ocr_reads_it_there(monkeypatch):
    """ apple-pay: the PassKit sheet is drawn outside the app, so 'Processing Payment' is not
    in the tree. OCR of the cited step's screenshot verifies it; a made-up quote still fails."""
    from tamam.executors.ios import grounder
    from tamam.executors.core.contracts import ActionSpace
    from tamam.executors.ios.hooks import StepContext
    from tamam.executors.ios.plan import ClauseLedger
    from tamam.harness.clauses import Clause, HeuristicPlanner
    monkeypatch.setattr(grounder, "ocr_lines", lambda p: ["Apple Pay", "Processing Payment", "Done"]
                        if p == "/shot4.png" else [])

    async def ledger(evidence):
        led = ClauseLedger(HeuristicPlanner(), preset=[Clause(
            id="c1", text="Verify the payment is authorised", status="done", step=4, evidence=evidence)])
        ctx = StepContext(objective="x", driver=None)
        ctx.step_num, ctx.shot_path = 4, "/shot4.png"
        ctx.space = ActionSpace(elements=[], text="", screen_w=390, screen_h=844, source_hash="h")
        await led.after_observe(ctx)
        return led.unsupported()

    assert await ledger('the sheet shows "Processing Payment"') == []
    assert [c.id for c in await ledger('the sheet shows "Payment approved"')] == ["c1"]


async def test_a_claim_that_a_switch_is_on_while_the_app_says_off_is_refused():
    """Breadth false pass (Flutter neg-ff-feature-default): the tree read e12 Switch (off); the
    agent called it 'on' from the screenshot and passed a test whose claim is false. The app's
    own state outranks the picture: the PASS is refused, and it can never stand."""
    from tamam.executors.ios.brain import Done
    from tamam.executors.core.contracts import ActionSpace, Element, Frame
    from tamam.executors.ios.hooks import StepContext
    from tamam.executors.ios.plan import ClauseLedger
    from tamam.harness.clauses import Clause, HeuristicPlanner

    async def gate(text, evidence, toggled):
        led = ClauseLedger(HeuristicPlanner(), preset=[Clause(
            id="c2", text=text, status="done", step=4, evidence=evidence)])
        ctx = StepContext(objective="x", driver=None)
        ctx.step_num = 4
        ctx.space = ActionSpace(elements=[
            Element(ref="e12", type="Switch", label="Date", frame=Frame(300, 600, 60, 30), toggled=toggled),
            Element(ref="e13", type="StaticText", label="Enable feature", frame=Frame(20, 600, 200, 30))],
            text="", screen_w=402, screen_h=874, source_hash="h")
        await led.after_observe(ctx)
        return await led.on_done(ctx, Done(passed=True, reason="r")), led

    claim = "Verify 'Enable feature' is switched on when the screen opens"
    msg, led = await gate(claim, "Switch e12 next to 'Enable feature' shows knob on right = on", False)
    assert msg and "reads (off)" in msg and "FAILED" in msg
    msg, _ = await gate(claim, "Switch e12 next to 'Enable feature' is on", True)
    assert msg is None                                   # app agrees: accepted
    msg, _ = await gate("Verify 'Enable feature' is switched off", "Enable feature (e12) is off", False)
    assert msg is None                                   # 'off' + off: consistent
    msg, _ = await gate("Verify 'Enable feature' is not switched on", "Enable feature (e12) is off", False)
    assert msg is None                                   # a NEGATED state claim, consistent: accepted
    msg, _ = await gate("Verify 'Enable feature' is not switched on", "Enable feature (e12) is on", True)
    assert msg and "reads (on)" in msg                   # negated claim, app says on: refused
    msg, _ = await gate("Verify 'Enable feature' is switched off", "Enable feature (e12) is off", True)
    assert msg and "reads (on)" in msg                   # 'off' claimed, app says on
    # After the gate gives up, a contradicted claim is marked: the verdict can't be a pass.
    _, led = await gate(claim, "e12 shows on", False)
    ctx = StepContext(objective="x", driver=None)
    ctx.refusals[led.name] = 99
    await led.on_done(ctx, Done(passed=True, reason="r"))
    assert led.clauses[0].unsupported
