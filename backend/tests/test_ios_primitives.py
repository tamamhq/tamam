"""P3 primitives: wait, back, clear-on-type, and settle-after-action."""

from tamam.executors.base import ExecutionSpec
from tamam.executors.ios.brain import Back, Done, Tap, Type, Wait
from tamam.executors.core.contracts import ActionSpace, Element, Frame
from tamam.executors.ios.executor import IOSAgentExecutor
from tests.test_ios_executor import FakeDeviceDriver, FakePerceiver, NoVision, ScriptedDecider


async def _noop(e): pass


class Driver(FakeDeviceDriver):
    async def type(self, el, text, clear=False): self.actions.append(("type", el.ref, text, clear))
    async def back(self): self.actions.append(("back",))


class Rec(ScriptedDecider):
    def __init__(self, a):
        super().__init__(a)
        self.histories = []

    async def decide(self, objective, space, history, screenshot):
        self.histories.append(list(history))
        return await super().decide(objective, space, history, screenshot)


def _spec():
    return ExecutionSpec(instructions="x", max_steps=6)


async def _run(actions, perceiver=None, driver=None):
    d, dec = driver or Driver(), Rec(actions)
    ex = IOSAgentExecutor(driver=d, perceiver=perceiver or FakePerceiver(), vision=NoVision(),
                          decider=dec)
    return await ex.execute(_spec(), on_event=_noop), d, dec


async def test_wait_pauses_and_is_capped(monkeypatch):
    import tamam.executors.ios.session as mod
    slept = []

    async def fake_sleep(s):
        slept.append(s)
    monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)
    _, _, dec = await _run([Wait(seconds=60), Done(passed=False, reason="r")])
    assert mod.MAX_WAIT_S in slept
    assert "waited 10s" in dec.histories[-1][0]


async def test_type_with_clear_replaces_the_value():
    _, d, _ = await _run([Type(ref="e2", text="new", clear=True), Done(passed=False, reason="r")])
    assert ("type", "e2", "new", True) in d.actions


async def test_back_prefers_the_screens_own_back_control():
    class WithBack:
        async def observe(self, driver):
            return ActionSpace(elements=[Element(ref="e1", type="Button", label="Back",
                                                 frame=Frame(0, 40, 40, 40))],
                               text="back", screen_w=390, screen_h=844, source_hash="A")
    _, d, dec = await _run([Back(), Done(passed=False, reason="r")], perceiver=WithBack())
    assert ("tap", "e1") in d.actions and ("back",) not in d.actions
    assert 'went back via "Back"' in dec.histories[-1][0]


async def test_back_falls_back_to_the_system_gesture():
    _, d, _ = await _run([Back(), Done(passed=False, reason="r")])
    assert ("back",) in d.actions


async def test_an_action_gets_a_settle_pause_before_the_next_read_not_a_second_read(monkeypatch):
    """A read taken mid-transition says 'did not change' and the agent retries (a double
    tap). The grace is a PAUSE before the step's one read — never a second tree read,
    which on list screens pages in more of the app every time."""
    import tamam.executors.ios.executor as mod
    slept = []
    real_sleep = mod.asyncio.sleep

    async def fake_sleep(s):
        slept.append(s)
        await real_sleep(0)
    monkeypatch.setattr(mod, "_SETTLE_S", 0.7)
    monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)

    class Counting(FakePerceiver):
        def __init__(self): self.n = 0
        async def observe(self, driver):
            self.n += 1
            return await super().observe(driver)
    p = Counting()
    await _run([Tap(ref="e1"), Done(passed=False, reason="r")], perceiver=p)
    assert 0.7 in slept
    assert p.n == 3  # step 1, step 2 (one read each), + the final end-state capture


def test_blank_spot_prefers_plain_text_below_the_nav_bar():
    from tamam.executors.core.contracts import ActionSpace, Element, Frame
    from tamam.executors.ios.session import blank_spot
    title = Element(ref="e1", type="StaticText", label="Sign up", frame=Frame(150, 50, 100, 30))
    heading = Element(ref="e2", type="StaticText", label="Step 1 of 3", frame=Frame(20, 140, 200, 30))
    sp = ActionSpace(elements=[title, heading], screen_w=402, screen_h=874)
    assert blank_spot(sp).label == "Step 1 of 3"          # not the nav bar's title


def test_blank_spot_falls_back_to_a_container_corner_then_the_margin():
    from tamam.executors.core.contracts import ActionSpace, Element, Frame
    from tamam.executors.ios.session import blank_spot
    form = Element(ref="e1", type="Other", label="form", frame=Frame(20, 150, 360, 300))
    spot = blank_spot(ActionSpace(elements=[form], screen_w=402, screen_h=874))
    assert spot.label == "form" and (spot.tap_x, spot.tap_y) == (32, 162)
    margin = blank_spot(ActionSpace(elements=[], screen_w=402, screen_h=874))
    assert margin.label == "the blank margin under the nav bar"
    covered = Element(ref="e1", type="Button", label="x", frame=Frame(0, 100, 402, 100))
    assert blank_spot(ActionSpace(elements=[covered], screen_w=402, screen_h=874)) is None
