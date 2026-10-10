"""The hook pipeline: quirks are composable parts, isolated from the loop."""

from tamam.executors.base import ExecutionSpec
from tamam.executors.ios.brain import Done, Tap
from tamam.executors.ios.executor import IOSAgentExecutor
from tamam.executors.ios.hooks import Hook
from tests.test_ios_executor import FakeDeviceDriver, FakePerceiver, NoVision, ScriptedDecider


async def _noop(e): pass


def _spec(n=5):
    return ExecutionSpec(instructions="x", max_steps=n)


async def test_a_broken_hook_is_skipped_not_fatal():
    class Broken(Hook):
        name = "broken"
        async def before_observe(self, ctx): raise RuntimeError("boom")
        async def after_observe(self, ctx): raise RuntimeError("boom")
        async def after_action(self, ctx, action, note): raise RuntimeError("boom")
        async def on_done(self, ctx, done): raise RuntimeError("boom")

    ex = IOSAgentExecutor(driver=FakeDeviceDriver(), perceiver=FakePerceiver(), vision=NoVision(),
                          decider=ScriptedDecider([Tap(ref="e1"), Done(passed=True, reason="ok")]),
                          hooks=[Broken()])
    result = await ex.execute(_spec(), on_event=_noop)
    assert result.error is None and result.self_verdict.passed is True


async def test_a_custom_hook_plugs_in_at_every_boundary():
    seen = []

    class Recorder(Hook):
        name = "recorder"
        async def before_observe(self, ctx): seen.append("before")
        async def after_observe(self, ctx): seen.append("after")
        async def after_action(self, ctx, action, note):
            seen.append("act")
            return note + " — recorded"
        async def on_done(self, ctx, done):
            seen.append("done")
            return None

    ex = IOSAgentExecutor(driver=FakeDeviceDriver(), perceiver=FakePerceiver(), vision=NoVision(),
                          decider=ScriptedDecider([Tap(ref="e1"), Done(passed=True, reason="ok")]),
                          hooks=[Recorder()])
    await ex.execute(_spec(), on_event=_noop)
    assert seen == ["before", "after", "act", "before", "after", "done"]


async def test_a_stop_gate_can_refuse_done_and_the_refusal_reaches_the_agent():
    class RefuseOnce(Hook):
        name = "refuse_once"
        async def on_done(self, ctx, done):
            return None if ctx.refusals[self.name] else "[gate] not yet"

    class Rec(ScriptedDecider):
        def __init__(self, a):
            super().__init__(a)
            self.histories = []
        async def decide(self, objective, space, history, screenshot):
            self.histories.append(list(history))
            return await super().decide(objective, space, history, screenshot)

    dec = Rec([Done(passed=True, reason="early"), Done(passed=True, reason="ok")])
    ex = IOSAgentExecutor(driver=FakeDeviceDriver(), perceiver=FakePerceiver(), vision=NoVision(),
                          decider=dec, hooks=[RefuseOnce()])
    result = await ex.execute(_spec(), on_event=_noop)
    assert result.self_verdict.reason == "ok"
    assert "[gate] not yet" in dec.histories[-1]


async def test_effect_note_lands_on_the_action_not_on_a_later_harness_note():
    """An alert report appended between an action and the next observation must not
    receive that action's 'screen did not change' annotation."""

    from tamam.executors.ios.driver import DriverTimeout

    class AlertAfterTap(FakeDeviceDriver):
        """The tap hangs behind a modal; the alert check that follows it reports one."""
        async def tap(self, el): raise DriverTimeout("tap timed out after 30s")
        async def dismiss_alert(self):
            return "Rate this app?"

    class Rec(ScriptedDecider):
        def __init__(self, a):
            super().__init__(a)
            self.histories = []
        async def decide(self, objective, space, history, screenshot):
            self.histories.append(list(history))
            return await super().decide(objective, space, history, screenshot)

    dec = Rec([Tap(ref="e1"), Done(passed=True, reason="ok")])
    ex = IOSAgentExecutor(driver=AlertAfterTap(), perceiver=FakePerceiver(), vision=NoVision(),
                          decider=dec)
    await ex.execute(_spec(), on_event=_noop)
    tap_note, alert_note = dec.histories[-1]
    assert tap_note.startswith("- [step 1] tap") or tap_note.startswith("[step 1] tap") and "did not change" in tap_note
    assert "Rate this app?" in alert_note and "did not change" not in alert_note


def test_swipes_on_different_screens_are_not_repeats():
    """A long scroll is many swipes from new positions, not one action repeated."""
    from tamam.executors.ios.brain import Swipe
    from tamam.executors.core.contracts import ActionSpace
    from tamam.executors.ios.hooks import fingerprint
    a, b = ActionSpace(source_hash="A"), ActionSpace(source_hash="B")
    assert fingerprint(Swipe(direction="up"), a) != fingerprint(Swipe(direction="up"), b)
    assert fingerprint(Swipe(direction="up"), a) == fingerprint(Swipe(direction="up"), a)


# ── switch effect: what a tap did to a switch, read from the app ──────────────────────

def _switch_space(on: bool, ref="e1"):
    from tamam.executors.core.contracts import ActionSpace, Element, Frame
    el = Element(ref=ref, type="Switch", label="Dark mode", frame=Frame(16, 168, 370, 54),
                 toggled=on)
    return ActionSpace(elements=[el], source_hash=f"h{int(on)}")


async def _tap_then_observe(before, after):
    from tamam.executors.ios.brain import Tap
    from tamam.executors.ios.hooks import EffectAnnotation, StepContext
    hook = EffectAnnotation()
    ctx = StepContext(objective="x", driver=None)
    ctx.space = before
    await hook.after_observe(ctx)                  # first read: no action yet
    ctx.history.append('[step 1] tapped e1 "Dark mode"')
    ctx.action_note_idx, ctx.last_action, ctx.space = 0, Tap(ref="e1"), after
    await hook.after_observe(ctx)
    return ctx.history[0]


async def test_a_flipped_switch_is_reported_with_its_new_state():
    note = await _tap_then_observe(_switch_space(False), _switch_space(True))
    assert 'switch now "Dark mode" on' in note


async def test_a_switch_that_snaps_back_reads_still_off():
    note = await _tap_then_observe(_switch_space(False), _switch_space(False))
    assert 'switch "Dark mode" is still off' in note
    assert "screen did not change" in note


def test_flipping_a_switch_back_and_forth_is_not_a_repeat():
    from tamam.executors.ios.brain import Tap
    from tamam.executors.ios.hooks import fingerprint
    assert fingerprint(Tap(ref="e1"), _switch_space(False)) != fingerprint(Tap(ref="e1"), _switch_space(True))


async def _type_then_observe(action):
    from tamam.executors.core.contracts import ActionSpace
    from tamam.executors.ios.hooks import EffectAnnotation, StepContext
    hook = EffectAnnotation()
    ctx = StepContext(objective="x", driver=None)
    same = ActionSpace(elements=[], source_hash="flat")
    ctx.space = same
    await hook.after_observe(ctx)
    ctx.prev_hash = "flat"
    ctx.history.append("[step 1] typed")
    ctx.action_note_idx, ctx.last_action, ctx.space = 0, action, same
    await hook.after_observe(ctx)
    return ctx.history[0]


async def test_typing_into_a_field_the_tree_cannot_show_is_not_called_rejected():
    """on a flattened form the tree never changes, so 'the field did NOT accept the text,
    do not retype it' was false (OCR read the text back) and steered the agent away."""
    from tamam.executors.ios.brain import Type
    blind = await _type_then_observe(Type(ref="", text="Ada"))
    assert "check the SCREENSHOT" in blind and "did NOT accept" not in blind
    seen = await _type_then_observe(Type(ref="e2", text="Ada"))
    assert "did NOT accept" in seen            # a field the tree shows still gets the warning


async def _unchanged_actions(n, limit=4, monkeypatch=None):
    """n actions in a row that leave the screen as it was; returns each action's note."""
    from tamam.config import get_settings
    from tamam.executors.core.contracts import ActionSpace
    from tamam.executors.ios.brain import LongPress
    from tamam.executors.ios.hooks import EffectAnnotation, StepContext
    monkeypatch.setattr(get_settings(), "no_progress_limit", limit)
    hook, ctx = EffectAnnotation(), StepContext(objective="hold to redeem", driver=None)
    same = ActionSpace(elements=[], source_hash="gift")
    ctx.space = same
    await hook.after_observe(ctx)
    for k in range(n):
        ctx.history.append(f"[step {k + 1}] long-pressed e4")
        ctx.action_note_idx, ctx.last_action, ctx.space = len(ctx.history) - 1, LongPress(ref="e4"), same
        await hook.after_observe(ctx)
    return ctx.history


async def test_the_agent_is_asked_to_conclude_two_unchanged_screens_before_the_guardrail(monkeypatch):
    """Varied retries (a longer hold each time) never trip the repetition warning; without this
    the NoProgress guardrail cut a plainly failing app off as inconclusive."""
    notes = await _unchanged_actions(3, monkeypatch=monkeypatch)
    warned = [i for i, n in enumerate(notes) if "Decide now" in n]
    assert warned == [1]                                   # once, at the 2nd unchanged screen
    assert "done(passed=false) citing those steps" in notes[1]    # the app ignored a reliable action
    assert 'blocked="missing_action"' in notes[1]          # unsure it was performed right


async def test_the_warning_follows_the_configured_limit(monkeypatch):
    notes = await _unchanged_actions(5, limit=6, monkeypatch=monkeypatch)
    assert [i for i, n in enumerate(notes) if "Decide now" in n] == [3]   # 6 - 2 unchanged screens
