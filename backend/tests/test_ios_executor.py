"""IOSAgentExecutor loop — proven hermetically with fakes for every seam.
No device, no Appium, no API key."""

import json

import pytest

from tamam.executors.base import ExecutionSpec
from tamam.executors.ios.brain import BackgroundApp, Done, Swipe, Tap, TapPoint, Type
from tamam.executors.core.contracts import ActionSpace, Element, Frame
from tamam.executors.ios.executor import IOSAgentExecutor


class FakeDeviceDriver:
    def __init__(self):
        self.actions = []
        self._errors = ["NSError: boom"]
        self._crashes = ["SIGABRT Sample"]

    async def start(self): self.actions.append("start")
    async def tap(self, el): self.actions.append(("tap", el.ref))
    async def type(self, el, text): self.actions.append(("type", el.ref, text))
    async def swipe(self, d): self.actions.append(("swipe", d))
    async def accessibility(self): return "<XCUIElementTypeApplication/>"
    async def screenshot(self): return "/tmp/ios_shot.png"
    def screen_size(self): return (390, 844)
    async def dismiss_alert(self): return False
    def logs(self): return list(self._errors), list(self._crashes)
    async def capture_diagnostics(self): pass
    async def stop(self): self.actions.append("stop"); return "/tmp/vid.mp4"


class FakePerceiver:
    async def observe(self, driver):
        return ActionSpace(
            elements=[
                Element(ref="e1", type="Button", label="Log In", frame=Frame(0, 0, 10, 10)),
                Element(ref="e2", type="TextField", label="Email", frame=Frame(0, 20, 10, 10)),
            ],
            text='[e1] Button "Log In"\n[e2] TextField "Email"',
            screen_w=390, screen_h=844, source_hash="X",
        )


class NoVision:
    def should_see(self, space): return False


class ScriptedDecider:
    def __init__(self, actions): self._a = list(actions); self.total_tokens = 7
    async def decide(self, objective, space, history, screenshot): return self._a.pop(0)


async def _noop(e): pass


def _executor(actions):
    return IOSAgentExecutor(
        driver=FakeDeviceDriver(), perceiver=FakePerceiver(),
        vision=NoVision(), decider=ScriptedDecider(actions),
    )


async def test_ios_loop_runs_actions_then_done():
    events = []

    async def collect(e): events.append(e)

    ex = _executor([Tap(ref="e1", reason="open"), Swipe(direction="up"), Done(passed=True, reason="ok")])
    result = await ex.execute(
        ExecutionSpec(instructions="log in", max_steps=10),
        on_event=collect,
    )
    assert [e.step for e in events] == [1, 2, 3]
    assert result.self_verdict is not None and result.self_verdict.passed is True
    assert result.error is None


async def test_ios_evidence_bundle():
    ex = _executor([Done(passed=False, reason="no button", repro_steps=["a", "b", "c"])])
    result = await ex.execute(
        ExecutionSpec(instructions="x", max_steps=5),
        on_event=_noop,
    )
    assert result.diagnostics.errors == ["NSError: boom"]       # device log evidence
    assert result.diagnostics.crashes == ["SIGABRT Sample"]     # crash evidence
    assert result.video_path == "/tmp/vid.mp4"
    assert result.usage.total_tokens == 7
    assert result.self_verdict.passed is False
    # settled end-state screenshot captured after the loop (for the judge)
    assert result.final_screenshot_path == "/tmp/ios_shot.png"


async def test_ios_invalid_ref_skipped_not_fatal():
    ex = _executor([Tap(ref="e99", reason="miss"), Done(passed=True, reason="ok")])
    result = await ex.execute(
        ExecutionSpec(instructions="x", max_steps=5),
        on_event=_noop,
    )
    assert result.error is None
    assert result.self_verdict.passed is True


def test_ios_agent_registered():
    from tamam.executors.registry import registered_executors

    assert "ios_agent" in registered_executors()


async def test_ios_executor_preserves_evidence_on_cancel():
    """When a harness guardrail cancels the run mid-flight, the executor must still
    RETURN the partial evidence (steps + their screenshots + the finalized video),
    not lose it — so screenshots/video are saved even on failure."""
    import asyncio

    from tamam.executors.ios.brain import Tap

    class CancelAfterFirst:
        total_tokens = 0

        def __init__(self):
            self.calls = 0

        async def decide(self, objective, space, history, screenshot):
            self.calls += 1
            if self.calls == 1:
                return Tap(ref="e1", reason="first action")
            await asyncio.Event().wait()  # hang until the guardrail cancels us

    ex = IOSAgentExecutor(driver=FakeDeviceDriver(), perceiver=FakePerceiver(),
                          vision=NoVision(), decider=CancelAfterFirst())
    events = []

    async def collect(e):
        events.append(e)

    task = asyncio.ensure_future(ex.execute(
        ExecutionSpec(instructions="x", max_steps=10),
        on_event=collect))
    for _ in range(100):  # let step 1 stream, then the decider hangs
        await asyncio.sleep(0.01)
        if events:
            break
    task.cancel()
    result = await task  # must return partial evidence, not raise

    assert len(result.steps) >= 1                       # steps preserved
    assert result.steps[0].screenshot_path == "/tmp/ios_shot.png"  # + screenshot
    assert result.video_path == "/tmp/vid.mp4"          # video finalized in `finally`
    assert result.error is None                         # harness stamps the guardrail error


async def test_repetition_warning_and_fingerprint():
    """Re-tapping the same control surfaces an anti-fixation note in history and stamps
    a stable action_fingerprint on each step (for the RepeatedAction guardrail)."""
    from tamam.executors.ios.brain import Done, Tap

    class RecordingDecider:
        total_tokens = 7

        def __init__(self, actions):
            self._a = list(actions)
            self.histories = []

        async def decide(self, objective, space, history, screenshot):
            self.histories.append(list(history))
            return self._a.pop(0)

    dec = RecordingDecider([Tap(ref="e1", reason="tap")] * 5 + [Done(passed=True, reason="ok")])
    ex = IOSAgentExecutor(driver=FakeDeviceDriver(), perceiver=FakePerceiver(),
                          vision=NoVision(), decider=dec)
    result = await ex.execute(
        ExecutionSpec(instructions="x", max_steps=10),
        on_event=_noop)

    # every tap step carries a stable fingerprint (label+coords, not the churning ref)
    fps = [s.action_fingerprint for s in result.steps if s.action_fingerprint]
    assert fps and all(f == fps[0] for f in fps)
    # by a later decide, history warns about the repetition
    joined = " ".join(dec.histories[-1])
    assert "already performed this same action" in joined


async def test_tap_surfaces_resulting_selected_label():
    """After a tap, history reports what is now active — so the agent can tell it
    activated the WRONG icon (its target label isn't the one now selected). This is
    the fix for icon-only chip fixation (Toys vs Books)."""
    from tamam.executors.ios.brain import Done, Tap

    class SelectingPerceiver:
        """First observation: nothing selected. After the tap: an SF-symbol chip is
        selected and resolves to the human label 'Books'."""
        def __init__(self):
            self._n = 0

        async def observe(self, driver):
            self._n += 1
            selected = self._n >= 2
            # unselected the chip is an opaque SF-symbol; once selected it reveals its
            # human label (as real apps do: Books, not square.grid.2x2)
            label = "Books" if selected else "square.grid.2x2"
            return ActionSpace(
                elements=[
                    Element(ref="e1", type="Button", label=label,
                            frame=Frame(0, 0, 10, 10), selected=selected),
                ],
                text=f'[e1] Button "{label}"',
                screen_w=390, screen_h=844, source_hash="X",
            )

    class RecordingDecider:
        total_tokens = 7

        def __init__(self, actions):
            self._a = list(actions)
            self.histories = []

        async def decide(self, objective, space, history, screenshot):
            self.histories.append(list(history))
            return self._a.pop(0)

    dec = RecordingDecider([Tap(ref="e1", reason="guess Toys"), Done(passed=False, reason="x")])
    ex = IOSAgentExecutor(driver=FakeDeviceDriver(), perceiver=SelectingPerceiver(),
                          vision=NoVision(), decider=dec)
    await ex.execute(
        ExecutionSpec(instructions="filter by Toys", max_steps=5),
        on_event=_noop)

    # the Done step's history shows the tap resolved to 'Books', not the target
    joined = " ".join(dec.histories[-1])
    assert 'now selected: "Books"' in joined


async def test_null_grounder_leaves_space_unchanged():
    """Default NullGrounder: the vision-fallback path adds nothing — a11y-only
    behavior is preserved (the brain sees exactly the perceiver's elements)."""
    from tamam.executors.ios.brain import Done, Tap
    from tamam.executors.ios.grounder import NullGrounder, merge_grounded
    from tamam.executors.core.contracts import ActionSpace, Element, Frame

    sp = ActionSpace(elements=[Element(ref="e1", type="Button", label="x", frame=Frame(0, 0, 5, 5))],
                     text='[e1] Button "x"', screen_w=390, screen_h=844)
    assert merge_grounded(sp, []) is sp  # no detections → identical object

    class AlwaysVision:
        def should_see(self, space): return True

    dec = ScriptedDecider([Tap(ref="e1", reason="t"), Done(passed=True, reason="ok")])
    ex = IOSAgentExecutor(driver=FakeDeviceDriver(), perceiver=FakePerceiver(),
                          vision=AlwaysVision(), decider=dec, grounder=NullGrounder())
    result = await ex.execute(
        ExecutionSpec(instructions="x", max_steps=5), on_event=_noop)
    assert result.self_verdict.passed


async def test_grounder_adds_tappable_target_missing_from_a11y():
    """A grounder that finds an icon the a11y tree missed makes it tappable: the merged
    element gets a fresh ref the brain can act on, grounded by its screen frame."""
    from tamam.executors.ios.brain import Done, Tap
    from tamam.executors.core.contracts import Frame, GroundedElement

    class FakeGrounder:
        async def detect(self, png, region, space):
            return [GroundedElement(label="Toys", frame=Frame(300, 70, 40, 40))]

    class AlwaysVision:
        def should_see(self, space): return True

    class ShotDriver(FakeDeviceDriver):
        async def screenshot(self):
            from PIL import Image
            p = "/tmp/ios_grounder_shot.png"
            Image.new("RGB", (390, 844)).save(p)  # real file for the grounder to read
            return p

    # brain taps e3 — a ref that only EXISTS because the grounder added it (perceiver
    # returns e1,e2). If the merge didn't happen, e3 wouldn't be found.
    driver = ShotDriver()
    dec = ScriptedDecider([Tap(ref="e3", reason="tap Toys"), Done(passed=True, reason="ok")])
    ex = IOSAgentExecutor(driver=driver, perceiver=FakePerceiver(),
                          vision=AlwaysVision(), decider=dec, grounder=FakeGrounder())
    result = await ex.execute(
        ExecutionSpec(instructions="filter by Toys", max_steps=5),
        on_event=_noop)
    assert result.self_verdict.passed
    assert ("tap", "e3") in driver.actions  # the vision-found target was actually tapped


async def test_driver_command_timeout_raises(tmp_path):
    """A hung Appium call fails fast with DriverTimeout instead of blocking forever."""
    import time
    import pytest
    from tamam.executors.ios.driver import AppiumDriver, DriverTimeout

    class HangSession:
        capabilities = {}
        @property
        def page_source(self):
            time.sleep(1)
            return "<x/>"

    d = AppiumDriver(session=HangSession(), artifacts_dir=str(tmp_path))
    d._cmd_timeout = 0.2
    d._read_timeout = 0.2  # accessibility() uses the read budget
    with pytest.raises(DriverTimeout):
        await d.accessibility()


async def test_executor_recovers_from_transient_observe_timeout():
    """A driver hang during observe is recovered — by taking a FRESH session, since the
    wedged one is still building the snapshot that timed out — and the run continues to a
    normal, judged completion rather than a lost verdict."""
    from tamam.executors.ios.brain import Done, Tap
    from tamam.executors.ios.driver import DriverTimeout

    class FlakyPerceiver:
        def __init__(self): self._n = 0
        async def observe(self, driver):
            self._n += 1
            if self._n == 1:  # first observe hangs, then heals
                raise DriverTimeout("accessibility timed out")
            return ActionSpace(
                elements=[Element(ref="e1", type="Button", label="Save", frame=Frame(0, 0, 5, 5))],
                text='[e1] Button "Save"', screen_w=390, screen_h=844, source_hash="X")

    dec = ScriptedDecider([Tap(ref="e1", reason="save"), Done(passed=True, reason="saved")])
    ex = IOSAgentExecutor(driver=FakeDeviceDriver(), perceiver=FlakyPerceiver(),
                          vision=NoVision(), decider=dec)

    async def _reconnect(_driver):   # a fresh session is available
        return True
    ex._try_reconnect = _reconnect
    result = await ex.execute(
        ExecutionSpec(instructions="x", max_steps=5), on_event=_noop)
    assert result.self_verdict and result.self_verdict.passed
    assert result.error is None  # recovered — not an infra error


async def test_executor_ends_gracefully_when_device_stays_wedged():
    """If the device never recovers, the loop ends with partial evidence and NO error,
    so the harness still judges what happened (a run that finished the task before the
    device died still gets a real verdict; a repeatedly-crashing app is itself a finding)."""
    from tamam.executors.ios.driver import DriverTimeout

    class DeadPerceiver:
        async def observe(self, driver):
            raise DriverTimeout("accessibility timed out")

    dec = ScriptedDecider([Done(passed=True, reason="never reached")])
    ex = IOSAgentExecutor(driver=FakeDeviceDriver(), perceiver=DeadPerceiver(),
                          vision=NoVision(), decider=dec)
    result = await ex.execute(
        ExecutionSpec(instructions="x", max_steps=5), on_event=_noop)
    assert result.error is None       # graceful end, not an infra error → gets judged
    assert result.self_verdict is None  # agent never concluded


async def test_executor_survives_action_timeout():
    """A tap that hangs is noted and the loop continues (re-observe next step), rather
    than aborting the whole run."""
    from tamam.executors.ios.brain import Done, Tap
    from tamam.executors.ios.driver import DriverTimeout

    class TapHangDriver(FakeDeviceDriver):
        async def tap(self, el):
            raise DriverTimeout("tap timed out")

    dec = ScriptedDecider([Tap(ref="e1", reason="t"), Done(passed=False, reason="done")])
    ex = IOSAgentExecutor(driver=TapHangDriver(), perceiver=FakePerceiver(),
                          vision=NoVision(), decider=dec)
    result = await ex.execute(
        ExecutionSpec(instructions="x", max_steps=5), on_event=_noop)
    assert result.error is None
    assert result.self_verdict is not None  # reached Done despite the tap hang


async def test_executor_reports_infra_error_on_decider_timeout(monkeypatch):
    """A wedged brain (LLM) call is OUR infra failing. It's bounded, and surfaces as
    result.error (→ ERROR, inconclusive) — NOT judged as partial evidence, which would risk
    a false FAIL of a fine app that simply wasn't finished being driven."""
    import asyncio as _asyncio
    from tamam.config import Settings
    from tamam.executors.ios.brain import Tap
    # Patch the name the executor actually binds (`from tamam.config import get_settings`),
    # not tamam.config.get_settings — the old test patched the wrong target, so the timeout
    # never fired and the test passed for the wrong reason. Use a real Settings so every
    # attribute the executor reads exists; only shrink the decider timeout.
    monkeypatch.setattr("tamam.executors.ios.executor.get_settings",
                        lambda: Settings(decider_timeout_s=1))

    class HangDecider:
        total_tokens = 0
        async def decide(self, *a, **k):
            await _asyncio.sleep(5)  # well past the 1s decider timeout
            return Tap(ref="e1", reason="never")

    ex = IOSAgentExecutor(driver=FakeDeviceDriver(), perceiver=FakePerceiver(),
                          vision=NoVision(), decider=HangDecider())
    result = await ex.execute(
        ExecutionSpec(instructions="x", max_steps=5), on_event=_noop)
    assert result.error is not None      # our-infra timeout → ERROR, never a judged false FAIL
    assert "infrastructure" in result.error.lower() or "inconclusive" in result.error.lower()
    assert result.self_verdict is None


async def test_executor_heartbeats_bracket_each_step():
    """Heartbeats fire around every driver span within a step (dismiss/observe/
    screenshot/apply) — so a slow-but-live step can't reach the stuck-watchdog budget."""
    from tamam.executors.ios.brain import Done, Tap

    calls = {"n": 0}
    async def hb():
        calls["n"] += 1

    dec = ScriptedDecider([Tap(ref="e1", reason="t"), Done(passed=True, reason="ok")])
    ex = IOSAgentExecutor(driver=FakeDeviceDriver(), perceiver=FakePerceiver(),
                          vision=NoVision(), decider=dec)
    await ex.execute(
        ExecutionSpec(instructions="x", max_steps=5),
        on_event=_noop, heartbeat=hb)
    # first step (Tap): dismiss+observe+som+apply = 4 heartbeats at minimum
    assert calls["n"] >= 4


async def test_type_dismisses_keyboard_after():
    """After a type, the executor hides the keyboard so the next observe's a11y tree
    isn't bloated (a cause of page_source timeouts). Best-effort/optional."""
    from tamam.executors.ios.brain import Done, Type

    class KbDriver(FakeDeviceDriver):
        def __init__(self):
            super().__init__()
            self.hid = 0
        async def hide_keyboard(self):
            self.hid += 1
            return True

    driver = KbDriver()
    # FakePerceiver exposes e2 as a TextField
    dec = ScriptedDecider([Type(ref="e2", text="hello", reason="t"), Done(passed=True, reason="ok")])
    ex = IOSAgentExecutor(driver=driver, perceiver=FakePerceiver(), vision=NoVision(), decider=dec)
    await ex.execute(
        ExecutionSpec(instructions="x", max_steps=5), on_event=_noop)
    assert driver.hid >= 1  # keyboard dismissed after the type


async def test_missing_hide_keyboard_is_noop():
    """A driver without hide_keyboard() must not break the loop (getattr guard)."""
    from tamam.executors.ios.brain import Done, Type
    # FakeDeviceDriver has no hide_keyboard → _safe_hide_keyboard is a no-op
    dec = ScriptedDecider([Type(ref="e2", text="hi", reason="t"), Done(passed=True, reason="ok")])
    ex = IOSAgentExecutor(driver=FakeDeviceDriver(), perceiver=FakePerceiver(),
                          vision=NoVision(), decider=dec)
    result = await ex.execute(
        ExecutionSpec(instructions="x", max_steps=5), on_event=_noop)
    assert result.self_verdict.passed


async def test_driver_tunes_perception_settings(tmp_path):
    """At start, the driver tunes WDA so page_source stays fast on heavy screens
    (capped depth, no idle wait) — the general fix for page_source timeouts."""
    from tamam.executors.ios.driver import AppiumDriver

    class RecSession:
        capabilities = {}
        def __init__(self): self.settings = None
        def update_settings(self, d): self.settings = d

    sess = RecSession()
    d = AppiumDriver(session=sess, artifacts_dir=str(tmp_path))
    await d._tune_perception()
    assert sess.settings["snapshotMaxDepth"] == 40
    # bounds how far a walk goes into an infinite list — a walk that reaches the end
    # fires "load more" and can cascade (see config.ios_snapshot_max_children)
    assert sess.settings["snapshotMaxChildren"] == 100
    assert sess.settings["waitForIdleTimeout"] == 0.2   # local simulator; farm devices keep 1.0
    assert sess.settings["customSnapshotTimeout"] >= 10


async def test_tune_perception_survives_unsupported(tmp_path):
    """A provider that rejects a setting must not break startup — best effort."""
    from tamam.executors.ios.driver import AppiumDriver

    class PickySession:
        capabilities = {}
        def update_settings(self, d): raise RuntimeError("setting not supported")

    d = AppiumDriver(session=PickySession(), artifacts_dir=str(tmp_path))
    await d._tune_perception()  # must not raise


async def test_accessibility_gets_larger_read_budget(tmp_path):
    """The tree read (page_source) uses the larger read budget, not the tight command
    timeout — so a slow-but-live heavy screen isn't force-timed-out."""
    import time
    from tamam.executors.ios.driver import AppiumDriver

    class SlowRead:
        capabilities = {}
        @property
        def page_source(self):
            time.sleep(0.5)
            return "<XCUIElementTypeApplication/>"

    d = AppiumDriver(session=SlowRead(), artifacts_dir=str(tmp_path))
    d._cmd_timeout = 0.2      # a tap would time out at 0.2s...
    d._read_timeout = 2.0     # ...but the read gets 2s, so it completes
    src = await d.accessibility()
    assert "XCUIElementType" in src


async def test_action_failure_is_soft_note_not_fatal():
    """An actuation error (e.g. type on a non-input → 'no text field') becomes an
    in-history note and the run continues to a verdict — not a fatal error."""
    from tamam.executors.ios.brain import Done, Type

    class NoFieldDriver(FakeDeviceDriver):
        async def type(self, el, text):
            raise RuntimeError("no text field found to type into")

    dec = ScriptedDecider([Type(ref="e2", text="x", reason="t"), Done(passed=False, reason="done")])
    ex = IOSAgentExecutor(driver=NoFieldDriver(), perceiver=FakePerceiver(),
                          vision=NoVision(), decider=dec)
    result = await ex.execute(
        ExecutionSpec(instructions="x", max_steps=5), on_event=_noop)
    assert result.error is None                 # not fatal
    assert result.self_verdict is not None      # reached Done


async def test_build_failure_returns_error_and_runs_cleanup():
    """If provisioning (build) fails, execute() returns an infra error (not raises) AND
    the provider's cleanup runs — so a cloud/remote session can't be orphaned."""
    class FailProvider:
        def __init__(self): self.cleaned = False
        async def driver(self, spec): raise RuntimeError("device acquire failed")
        async def cleanup(self): self.cleaned = True

    prov = FailProvider()
    ex = IOSAgentExecutor(provider=prov, perceiver=FakePerceiver(),
                          vision=NoVision(), decider=ScriptedDecider([]))
    result = await ex.execute(
        ExecutionSpec(instructions="x", max_steps=5), on_event=_noop)
    assert result.error and "device acquire failed" in result.error
    assert result.self_verdict is None
    assert prov.cleaned is True  # session/provider cleanup ran despite the build failure


def test_safe_logs_tolerates_none_driver():
    ex = IOSAgentExecutor(driver=FakeDeviceDriver(), perceiver=FakePerceiver(),
                          vision=NoVision(), decider=ScriptedDecider([]))
    assert ex._safe_logs(None) == ([], [])


async def test_driver_raises_session_dead_and_reconnect_hook(tmp_path):
    from tamam.executors.ios.driver import AppiumDriver, DriverSessionDead

    class DeadSession:
        capabilities = {}
        @property
        def page_source(self):
            raise Exception("invalid session id: Session does not exist")

    d = AppiumDriver(session=DeadSession(), artifacts_dir=str(tmp_path))
    import pytest
    with pytest.raises(DriverSessionDead):
        await d.accessibility()
    called = {}
    async def hook(): called["ok"] = True
    d._reconnect = hook
    await d.reconnect()
    assert called["ok"]


class _ReconnectDriver(FakeDeviceDriver):
    def __init__(self):
        super().__init__()
        self.reconnects = 0
    async def reconnect(self):
        self.reconnects += 1


async def test_executor_recovers_from_session_death_and_continues():
    from tamam.executors.ios.brain import Done, Tap
    from tamam.executors.ios.driver import DriverSessionDead

    class FlakyPerceiver:
        def __init__(self): self.n = 0
        async def observe(self, driver):
            self.n += 1
            if self.n == 1:
                raise DriverSessionDead("invalid session id")  # app crashed
            return ActionSpace(
                elements=[Element(ref="e1", type="Button", label="x", frame=Frame(0, 0, 5, 5))],
                text='[e1] Button "x"', screen_w=390, screen_h=844, source_hash="X")

    drv = _ReconnectDriver()
    dec = ScriptedDecider([Tap(ref="e1", reason="t"), Done(passed=True, reason="ok")])
    ex = IOSAgentExecutor(driver=drv, perceiver=FlakyPerceiver(), vision=NoVision(), decider=dec)
    result = await ex.execute(
        ExecutionSpec(instructions="x", max_steps=5), on_event=_noop)
    assert drv.reconnects == 1              # reconnected once
    assert result.self_verdict.passed       # then ran to completion
    assert result.error is None


async def test_executor_ends_gracefully_when_session_keeps_dying():
    from tamam.executors.ios.brain import Done
    from tamam.executors.ios.driver import DriverSessionDead

    class DeadPerceiver:
        async def observe(self, driver):
            raise DriverSessionDead("invalid session id")  # crashes every time

    drv = _ReconnectDriver()
    ex = IOSAgentExecutor(driver=drv, perceiver=DeadPerceiver(), vision=NoVision(),
                          decider=ScriptedDecider([Done(passed=True, reason="x")]))
    result = await ex.execute(
        ExecutionSpec(instructions="x", max_steps=10), on_event=_noop)
    assert 1 <= drv.reconnects <= 2         # bounded by the cap
    assert result.error is None             # graceful end → judged, not a bare error


async def test_tap_point_then_ref_less_type_targets_focused_field():
    """The coordinate-tap fallback: tap_point (normalized coords → pixels) focuses a field
    with no ref, then a ref-less `type` enters text into that focused field. This is the
    path for rich-editor content (e.g. a populated rich-text title) the a11y tree omits."""
    from tamam.executors.ios.brain import Done, Type

    class RecordingDriver(FakeDeviceDriver):
        def __init__(self):
            super().__init__()
            self.points = []
            self.focused_typed = []
        async def tap_point(self, x, y):
            self.points.append((x, y))
        async def type_focused(self, text, clear=False):
            self.focused_typed.append(text)

    drv = RecordingDriver()
    dec = ScriptedDecider([
        TapPoint(x=0.5, y=0.25, reason="tap the title I can see but has no ref"),
        Type(ref="", text="New Title", reason="type into the now-focused title"),
        Done(passed=True, reason="title edited"),
    ])
    ex = IOSAgentExecutor(driver=drv, perceiver=FakePerceiver(), vision=NoVision(), decider=dec)
    result = await ex.execute(
        ExecutionSpec(instructions="x", max_steps=5), on_event=_noop)
    # FakePerceiver screen is 390x844 → (0.5,0.25) → (195, 211)
    assert drv.points == [(195, 211)]
    assert drv.focused_typed == ["New Title"]
    assert result.self_verdict.passed


async def test_start_with_reconnect_recovers_from_stale_startup_session():
    """The executor recovers when a cloud device returns a session that's dead at startup:
    start() raises DriverSessionDead once, we reconnect on the same endpoint, retry start(),
    and proceed — instead of erroring the run with 0 steps (the AWS Device Farm symptom)."""
    from tamam.executors.ios.driver import DriverSessionDead
    from tamam.executors.ios.executor import IOSAgentExecutor

    class FlakyStartDriver:
        def __init__(self):
            self.starts = 0
            self.reconnects = 0

        async def start(self):
            self.starts += 1
            if self.starts == 1:
                raise DriverSessionDead("get-window-size: invalid session id")

        async def reconnect(self):
            self.reconnects += 1

    ex = IOSAgentExecutor()
    ex._reconnects = 0
    ex._max_reconnects = 2
    drv = FlakyStartDriver()
    await ex._start_with_reconnect(drv)
    assert drv.starts == 2 and drv.reconnects == 1


async def test_start_with_reconnect_raises_when_budget_exhausted():
    """If reconnect can't recover within budget, the DriverSessionDead propagates so the run
    ERRORs cleanly (never a silent hang or false verdict)."""
    from tamam.executors.ios.driver import DriverSessionDead
    from tamam.executors.ios.executor import IOSAgentExecutor

    class AlwaysDeadDriver:
        async def start(self):
            raise DriverSessionDead("get-window-size: invalid session id")

        async def reconnect(self):
            pass

    ex = IOSAgentExecutor()
    ex._reconnects = 0
    ex._max_reconnects = 1
    with pytest.raises(DriverSessionDead):
        await ex._start_with_reconnect(AlwaysDeadDriver())


# --- premature-conclusion guard -------------------------------------------------
# A negative verdict asserts absence. Absence cannot be asserted while the screen
# itself proves there is content the agent has never seen (a side-clipped row).

class ClippedPerceiver:
    """A screen whose row is cut off at the right edge — more items exist off-view."""
    def __init__(self): self.calls = 0

    async def observe(self, driver):
        self.calls += 1
        return ActionSpace(
            elements=[
                Element(ref="e1", type="Button", label="alpha", frame=Frame(0, 0, 10, 10)),
                Element(ref="e2", type="Button", label="omega", frame=Frame(380, 0, 40, 10),
                        edge_cropped="right"),
            ],
            text='[e1] Button "alpha"\n[e2] Button "omega" (cut off at the right edge)',
            screen_w=390, screen_h=844, source_hash=f"S{self.calls}",
        )




class RecordingSwipeDriver(FakeDeviceDriver):
    """Captures which element a swipe was aimed at."""
    def __init__(self): super().__init__(); self.swipes = []
    async def swipe(self, d, element=None):
        self.swipes.append((d, element.ref if element is not None else None))


async def test_swipe_can_be_aimed_at_a_row_not_just_the_window():
    """A row-swipe must reach the driver WITH its element: an unaimed swipe hits the
    page body, which is how a horizontal strip becomes unreachable."""
    drv = RecordingSwipeDriver()
    ex = IOSAgentExecutor(
        driver=drv, perceiver=ClippedPerceiver(), vision=NoVision(),
        decider=ScriptedDecider([
            Swipe(direction="left", ref="e2", reason="scroll the row"),
            Done(passed=True, reason="done"),
        ]),
    )
    await ex.execute(ExecutionSpec(instructions="x"), _noop)
    assert drv.swipes == [("left", "e2")], "the swipe must carry the row it targets"


async def test_unaimed_swipe_still_works():
    """Window-level swipes remain valid — vertical scrolling must not need a ref."""
    drv = RecordingSwipeDriver()
    ex = IOSAgentExecutor(
        driver=drv, perceiver=ClippedPerceiver(), vision=NoVision(),
        decider=ScriptedDecider([
            Swipe(direction="up", reason="scroll the page"),
            Done(passed=True, reason="done"),
        ]),
    )
    await ex.execute(ExecutionSpec(instructions="x"), _noop)
    assert drv.swipes == [("up", None)]


# --- learning what an icon-only control is, by selecting it ----------------------

class RevealingPerceiver:
    """A chip strip whose chips name their ASSET until selected, at which point the
    selected one renders its real name — the common icon-only pattern."""
    def __init__(self): self.calls = 0; self.selected = False

    async def observe(self, driver):
        self.calls += 1
        label = "Books" if self.selected else "square.grid.2x2"
        return ActionSpace(
            elements=[
                Element(ref="e1", type="Button", label="shippingbox", frame=Frame(0, 100, 40, 30)),
                Element(ref="e2", type="Button", label=label, frame=Frame(50, 100, 40, 30)),
            ],
            text=f'[e1] Button "shippingbox"\n[e2] Button "{label}"',
            screen_w=390, screen_h=844, source_hash=f"S{self.calls}{self.selected}",
        )


class SelectingDriver(FakeDeviceDriver):
    def __init__(self, per): super().__init__(); self._per = per
    async def tap(self, el):
        if el.ref == "e2":
            self._per.selected = True


async def test_icon_meaning_is_learned_by_selecting_it():
    per = RevealingPerceiver()
    spaces = []

    class Spy(ScriptedDecider):
        async def decide(self, objective, space, history, screenshot):
            spaces.append(space.text)
            return await super().decide(objective, space, history, screenshot)

    ex = IOSAgentExecutor(
        driver=SelectingDriver(per), perceiver=per, vision=NoVision(),
        decider=Spy([Tap(ref="e2", reason="what is this?"),
                     Tap(ref="e1", reason="next"),
                     Done(passed=True, reason="ok")]),
    )
    await ex.execute(ExecutionSpec(instructions="x"), _noop)
    assert 'square.grid.2x2 = "Books"' in spaces[-1], \
        "once selected, the icon's real meaning must be carried forward"
    assert "square.grid.2x2" not in spaces[0] or "=" not in spaces[0], "nothing is known up front"


async def test_readable_labels_are_never_treated_as_discoveries():
    """The legend is for opaque controls only — it must not fill up with normal labels."""
    per = FakePerceiver()   # "Log In" / "Email": ordinary labels
    spaces = []

    class Spy(ScriptedDecider):
        async def decide(self, objective, space, history, screenshot):
            spaces.append(space.text)
            return await super().decide(objective, space, history, screenshot)

    ex = IOSAgentExecutor(
        driver=FakeDeviceDriver(), perceiver=per, vision=NoVision(),
        decider=Spy([Tap(ref="e1", reason="tap"), Done(passed=True, reason="ok")]),
    )
    await ex.execute(ExecutionSpec(instructions="x"), _noop)
    assert "already established" not in spaces[-1]


class PopupThenRevealPerceiver:
    """A popup lands between the tap and the result, so the reveal is on a LATER
    screen — the ordinary case on real apps, not an edge case."""
    def __init__(self): self.calls = 0; self.tapped = False

    async def observe(self, driver):
        self.calls += 1
        if self.tapped and self.calls == 3:       # the interloper
            return ActionSpace(
                elements=[Element(ref="e1", type="Button", label="Not now",
                                  frame=Frame(100, 400, 80, 30))],
                text='[e1] Button "Not now"', screen_w=390, screen_h=844,
                source_hash=f"P{self.calls}")
        label = "Books" if self.tapped else "square.grid.2x2"
        return ActionSpace(
            elements=[Element(ref="e1", type="Button", label=label, frame=Frame(50, 100, 40, 30))],
            text=f'[e1] Button "{label}"', screen_w=390, screen_h=844,
            source_hash=f"S{self.calls}{self.tapped}")


class TapMarksPerceiver(FakeDeviceDriver):
    def __init__(self, per): super().__init__(); self._per = per
    async def tap(self, el): self._per.tapped = True


async def test_reveal_is_still_caught_when_a_popup_intervenes():
    per = PopupThenRevealPerceiver()
    spaces = []

    class Spy(ScriptedDecider):
        async def decide(self, objective, space, history, screenshot):
            spaces.append(space.text)
            return await super().decide(objective, space, history, screenshot)

    ex = IOSAgentExecutor(
        driver=TapMarksPerceiver(per), perceiver=per, vision=NoVision(),
        decider=Spy([Tap(ref="e1", reason="probe"), Tap(ref="e1", reason="dismiss"),
                     Tap(ref="e1", reason="back"), Done(passed=True, reason="ok")]),
    )
    await ex.execute(ExecutionSpec(instructions="x"), _noop)
    assert any('square.grid.2x2 = "Books"' in t for t in spaces), \
        "a popup between tap and result must not lose the discovery"


class LifecycleDriver(FakeDeviceDriver):
    def __init__(self): super().__init__(); self.backgrounded = []
    async def background_app(self, seconds=2.0): self.backgrounded.append(seconds)


async def test_agent_can_background_and_reopen_the_app():
    """Lifecycle is a capability, not a gesture: without it, 'does this survive
    backgrounding?' is unanswerable and the agent silently skips the check."""
    drv = LifecycleDriver()
    ex = IOSAgentExecutor(
        driver=drv, perceiver=FakePerceiver(), vision=NoVision(),
        decider=ScriptedDecider([BackgroundApp(seconds=1.5, reason="check persistence"),
                                 Done(passed=True, reason="survived")]),
    )
    res = await ex.execute(ExecutionSpec(instructions="x"), _noop)
    assert drv.backgrounded == [1.5]
    assert res.self_verdict.passed is True


def test_every_action_is_advertised_in_the_prompt_menu():
    """The menu IS the contract: an action the model is never shown is an action it
    will never choose, however well the schema allows it. This caught background_app
    and the swipe `ref` silently going unused on real runs."""
    import typing
    from tamam.executors.ios.brain import SYSTEM, NextAction
    menu = SYSTEM.split("## Actions (choose exactly ONE)")[1].split("\n\n")[0]
    for member in typing.get_args(NextAction):
        name = member.model_fields["action"].default
        assert name in menu, f"action {name!r} is missing from the prompt's action menu"


# --- a blank screen is a transition, not a state --------------------------------

class BlankThenLoadedPerceiver:
    """The first look catches the app mid-transition (no elements); it settles after."""
    def __init__(self, blanks=1): self.calls = 0; self.blanks = blanks

    async def observe(self, driver):
        self.calls += 1
        if self.calls <= self.blanks:
            return ActionSpace(elements=[], text="", screen_w=390, screen_h=844,
                               source_hash="da39a3ee5e")
        return ActionSpace(
            elements=[Element(ref="e1", type="Button", label="Continue", frame=Frame(0, 0, 10, 10))],
            text='[e1] Button "Continue"', screen_w=390, screen_h=844, source_hash="LOADED")


async def test_model_is_never_asked_to_act_on_a_blank_screen():
    per = BlankThenLoadedPerceiver(blanks=1)
    seen = []

    class Spy(ScriptedDecider):
        async def decide(self, objective, space, history, screenshot):
            seen.append(len(space.elements))
            return await super().decide(objective, space, history, screenshot)

    ex = IOSAgentExecutor(driver=FakeDeviceDriver(), perceiver=per, vision=NoVision(),
                          decider=Spy([Done(passed=True, reason="ok")]))
    await ex.execute(ExecutionSpec(instructions="x"), _noop)
    assert seen and all(n > 0 for n in seen), "a blank tree must be re-observed, not reasoned about"


async def test_a_persistently_blank_screen_still_proceeds():
    """Bounded: if the screen really is empty, the run continues rather than hanging."""
    per = BlankThenLoadedPerceiver(blanks=99)
    ex = IOSAgentExecutor(driver=FakeDeviceDriver(), perceiver=per, vision=NoVision(),
                          decider=ScriptedDecider([Done(passed=False, reason="nothing there"),
                                                   Done(passed=False, reason="nothing there")]))
    res = await ex.execute(ExecutionSpec(instructions="x"), _noop)
    assert res.self_verdict.passed is False


async def test_verdict_formed_on_a_blank_screen_is_re_looked():
    """The failure this came from: the run declared done on an empty screen at step 21."""
    per = BlankThenLoadedPerceiver(blanks=99)   # always blank → verdict is unobserved
    ex = IOSAgentExecutor(driver=FakeDeviceDriver(), perceiver=per, vision=NoVision(),
                          decider=ScriptedDecider([Done(passed=True, reason="claimed"),
                                                   Done(passed=True, reason="claimed again")]))
    res = await ex.execute(ExecutionSpec(instructions="x"), _noop)
    dones = [s for s in res.steps if s.actions and s.actions[0].get("action") == "done"]
    assert len(dones) == 2, "the first, unobserved verdict must not be accepted outright"


# --- a PASS that skipped a required action is not evidence ----------------------

async def test_pass_is_refused_when_a_required_action_never_happened():
    """The false pass we caught on a real run: the agent reported that a filter
    survived backgrounding, having never backgrounded anything."""
    ex = IOSAgentExecutor(
        driver=LifecycleDriver(), perceiver=FakePerceiver(), vision=NoVision(),
        decider=ScriptedDecider([
            Done(passed=True, reason="survived backgrounding"),      # refused
            BackgroundApp(seconds=1.0, reason="actually do it"),
            Done(passed=True, reason="survived backgrounding"),      # now earned
        ]),
    )
    res = await ex.execute(ExecutionSpec(instructions="select Books, send the app to the background and bring it back, "
                     "then verify Books is still selected"), _noop)
    dones = [s for s in res.steps if s.actions and s.actions[0].get("action") == "done"]
    assert len(dones) == 2
    assert res.self_verdict.passed is True


async def test_a_failing_verdict_is_never_gated_on_required_actions():
    """Not finishing is already what a FAIL reports — gating it would just burn steps."""
    ex = IOSAgentExecutor(
        driver=LifecycleDriver(), perceiver=FakePerceiver(), vision=NoVision(),
        decider=ScriptedDecider([Done(passed=False, reason="could not select Books")]),
    )
    res = await ex.execute(ExecutionSpec(instructions="send the app to the background and verify Books is still selected"), _noop)
    assert res.self_verdict.reason == "could not select Books"


async def test_objectives_that_need_no_special_action_are_untouched():
    ex = IOSAgentExecutor(
        driver=FakeDeviceDriver(), perceiver=FakePerceiver(), vision=NoVision(),
        decider=ScriptedDecider([Done(passed=True, reason="filter applied")]),
    )
    res = await ex.execute(ExecutionSpec(instructions="select the Electronics category and verify it filters the feed"), _noop)
    assert res.self_verdict.passed is True


# --- keyboard cost, and wedged sessions -----------------------------------------

class KeyboardTrackingDriver(FakeDeviceDriver):
    def __init__(self): super().__init__(); self.hides = 0
    async def hide_keyboard(self): self.hides += 1; return True


async def test_keyboard_is_put_away_before_snapshotting():
    """The keyboard is ~80x the tree and contributes no usable elements, so a keyboard
    the last read SHOWED is put away before the next snapshot (unless mid-typing)."""
    drv = KeyboardTrackingDriver()

    class KeyboardUp(FakePerceiver):
        async def observe(self, driver):
            space = await super().observe(driver)
            space.keyboard = True
            return space

    ex = IOSAgentExecutor(driver=drv, perceiver=KeyboardUp(), vision=NoVision(),
                          decider=ScriptedDecider([Tap(ref="e1", reason="tap"),
                                                   Done(passed=True, reason="ok")]))
    await ex.execute(ExecutionSpec(instructions="x"), _noop)
    assert drv.hides >= 1


async def test_keyboard_is_left_alone_while_typing():
    """`type` needs its field focused, so the observe that FOLLOWS a type must not
    reach in and dismiss the keyboard again."""
    drv = KeyboardTrackingDriver()
    seen: list[int] = []

    class Watch(FakePerceiver):
        async def observe(self, driver):
            seen.append(drv.hides)          # hides counted BEFORE this observe
            return await super().observe(driver)

    ex = IOSAgentExecutor(driver=drv, perceiver=Watch(), vision=NoVision(),
                          decider=ScriptedDecider([Type(ref="e2", text="hi", reason="type"),
                                                   Done(passed=True, reason="ok")]))
    await ex.execute(ExecutionSpec(instructions="x"), _noop)
    # seen[0]: before any action. seen[1]: after the type — `type` hides once itself,
    # and the pre-observe hook must NOT add another on top of it.
    assert len(seen) >= 2 and seen[1] - seen[0] == 1, seen


async def test_no_keyboard_means_no_hide_query():
    """A blind hide each step is a device query that walks the whole tree (on a real app
    each walk paged in more rows). No keyboard on the last read → no hide."""
    drv = KeyboardTrackingDriver()
    ex = IOSAgentExecutor(driver=drv, perceiver=FakePerceiver(), vision=NoVision(),
                          decider=ScriptedDecider([Tap(ref="e1"), Tap(ref="e1"),
                                                   Done(passed=True, reason="ok")]))
    await ex.execute(ExecutionSpec(instructions="x"), _noop)
    assert drv.hides == 0


class WedgedThenFreshPerceiver:
    """First observe wedges; only a NEW session can succeed."""
    def __init__(self): self.calls = 0; self.reconnected = False

    async def observe(self, driver):
        self.calls += 1
        if not self.reconnected:
            from tamam.executors.ios.driver import DriverTimeout
            raise DriverTimeout("accessibility timed out after 60s")
        return ActionSpace(
            elements=[Element(ref="e1", type="Button", label="Go", frame=Frame(0, 0, 10, 10))],
            text='[e1] Button "Go"', screen_w=390, screen_h=844, source_hash="OK")


async def test_a_wedged_session_is_replaced_not_retried():
    """Appium is one-command-at-a-time: after a timeout the session is still building
    that snapshot, so a retry queues behind it and adds only silence."""
    per = WedgedThenFreshPerceiver()
    ex = IOSAgentExecutor(driver=FakeDeviceDriver(), perceiver=per, vision=NoVision(),
                          decider=ScriptedDecider([Done(passed=True, reason="ok")]))

    async def fake_reconnect(driver):
        per.reconnected = True
        return True

    ex._try_reconnect = fake_reconnect
    res = await ex.execute(ExecutionSpec(instructions="x"), _noop)
    assert per.reconnected, "a wedged session must be replaced"
    assert res.self_verdict is not None and res.self_verdict.passed is True


async def test_a_verdict_is_not_lost_to_slow_teardown():
    """The run that prompted this: the agent reached `done`, then evidence gathering on a
    degraded app took minutes, the stuck-watchdog fired, and an earned verdict became
    ERROR. Teardown must keep signalling liveness."""
    beats: list[int] = []

    class SlowTeardownDriver(FakeDeviceDriver):
        def __init__(self): super().__init__(); self.reads = 0
        async def accessibility(self):
            self.reads += 1
            return f"<XCUIElementTypeApplication n='{self.reads}'/>"

    drv = SlowTeardownDriver()
    ex = IOSAgentExecutor(driver=drv, perceiver=FakePerceiver(), vision=NoVision(),
                          decider=ScriptedDecider([Done(passed=True, reason="done")]))

    async def beat():
        beats.append(1)

    res = await ex.execute(ExecutionSpec(instructions="x"),
                           _noop, heartbeat=beat)
    assert res.self_verdict.passed is True
    # the loop itself beats a few times; what matters is that beats continue AFTER the
    # verdict, while the expensive final capture runs.
    assert len(beats) >= 4, f"teardown must heartbeat; saw {len(beats)}"


# ── harness side effects are reported; "stuck" decays; model retries ──


class _HistoryDecider:
    total_tokens = 0

    def __init__(self, actions):
        self._a = list(actions)
        self.histories = []

    async def decide(self, objective, space, history, screenshot):
        self.histories.append(list(history))
        return self._a.pop(0)


async def test_auto_accepted_alert_is_reported_to_the_agent():
    """The harness accepting an alert is an action taken on the agent's behalf — it must
    appear in the history, or the agent reasons about an unexplained screen change."""

    from tamam.executors.ios.driver import DriverTimeout

    class AlertOnceDriver(FakeDeviceDriver):
        """A tap hangs behind a modal; the alert check that follows clears it."""
        def __init__(self):
            super().__init__()
            self._alerts = ["Allow notifications?"]

        async def tap(self, el): raise DriverTimeout("tap timed out after 30s")

        async def dismiss_alert(self):
            return self._alerts.pop(0) if self._alerts else None

    dec = _HistoryDecider([Tap(ref="e1", reason="t"), Done(passed=True, reason="ok")])
    ex = IOSAgentExecutor(driver=AlertOnceDriver(), perceiver=FakePerceiver(),
                          vision=NoVision(), decider=dec)
    await ex.execute(ExecutionSpec(instructions="x", max_steps=5),
                     on_event=_noop)
    assert any("Allow notifications?" in h and "accepted" in h for h in dec.histories[-1])


async def test_no_alert_query_on_a_normal_step():
    """A per-step alert query walks the whole tree (system dialogs are handled in the read).
    It runs only after an action timed out (a modal may be blocking the session)."""
    class CountingDriver(FakeDeviceDriver):
        def __init__(self):
            super().__init__()
            self.alert_checks = 0
        async def dismiss_alert(self):
            self.alert_checks += 1
            return None

    d = CountingDriver()
    ex = IOSAgentExecutor(driver=d, perceiver=FakePerceiver(), vision=NoVision(),
                          decider=ScriptedDecider([Tap(ref="e1"), Done(passed=True, reason="ok")]))
    await ex.execute(ExecutionSpec(instructions="x", max_steps=5),
                     on_event=_noop)
    assert d.alert_checks == 0


class _ChangingPerceiver:
    """Every observation is a new screen, so only REPETITION can make the run 'stuck'."""

    def __init__(self):
        self.n = 0

    async def observe(self, driver):
        self.n += 1
        return ActionSpace(
            elements=[Element(ref=f"e{i}", type="Button", label=f"B{i}",
                              frame=Frame(0, i * 40, 10, 10)) for i in range(1, 7)],
            text="buttons", screen_w=390, screen_h=844, source_hash=f"S{self.n}")


async def test_stuck_hint_decays_after_the_repetition_ends():
    """One early repeat must not mark the rest of the run stuck (the hint used to be
    re-added on EVERY later step, crowding real steps out of the history window)."""
    acts = [Tap(ref="e1"), Tap(ref="e1")] + [Tap(ref=f"e{i}") for i in range(2, 7)]
    dec = _HistoryDecider(acts + [Done(passed=True, reason="ok")])
    ex = IOSAgentExecutor(driver=FakeDeviceDriver(), perceiver=_ChangingPerceiver(),
                          vision=NoVision(), decider=dec)
    await ex.execute(ExecutionSpec(instructions="x", max_steps=20),
                     on_event=_noop)
    hints = [h for h in dec.histories[-1] if h.startswith("[hint]")]
    assert len(hints) == 1


async def test_transient_model_error_is_retried_not_fatal(monkeypatch):
    import tamam.executors.ios.executor as mod
    monkeypatch.setattr(mod, "_DECIDE_BACKOFF_S", 0)

    class FlakyDecider:
        total_tokens = 0

        def __init__(self):
            self.calls = 0

        async def decide(self, objective, space, history, screenshot):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("529 overloaded")
            return Done(passed=True, reason="ok")

    dec = FlakyDecider()
    ex = IOSAgentExecutor(driver=FakeDeviceDriver(), perceiver=FakePerceiver(),
                          vision=NoVision(), decider=dec)
    result = await ex.execute(
        ExecutionSpec(instructions="x", max_steps=5),
        on_event=_noop)
    assert result.error is None and result.self_verdict.passed is True
    assert dec.calls == 2


async def test_a_timed_out_action_does_not_queue_extra_commands():
    """After an action times out, the session is still busy with it; every extra command
    queues and burns a full timeout. No keyboard call until an observation gets through."""
    from tamam.executors.ios.driver import DriverTimeout

    class SlowTapDriver(FakeDeviceDriver):
        def __init__(self):
            super().__init__()
            self.kb = 0
        async def tap(self, el): raise DriverTimeout("tap timed out after 30s")
        async def hide_keyboard(self):
            self.kb += 1
            self.actions.append(("hide_keyboard",))
            return True

    d = SlowTapDriver()
    ex = IOSAgentExecutor(driver=d, perceiver=FakePerceiver(), vision=NoVision(),
                          decider=ScriptedDecider([Tap(ref="e1"), Done(passed=False, reason="r")]))
    await ex.execute(ExecutionSpec(instructions="x", max_steps=5),
                     on_event=_noop)
    # no keyboard was ever on screen, and after the timed-out tap nothing extra is queued
    assert d.kb == 0


async def test_every_step_is_traced_with_phases_and_decision():
    """The trace answers 'why is the screen static?': per-phase seconds, the agent's
    action, and what the harness appended — one record per step."""
    ex = _executor([Tap(ref="e1", reason="open"), Done(passed=True, reason="ok")])
    result = await ex.execute(
        ExecutionSpec(instructions="x", max_steps=5), on_event=_noop)
    trace = result.usage_extra["step_trace"]
    assert [t["step"] for t in trace] == [1, 2]
    assert {"observe", "decide", "act"} <= set(trace[0]["diag"]["phases"])
    assert trace[0]["action"]["action"] == "tap" and trace[0]["notes"]


async def test_a_keyboard_that_cannot_be_hidden_is_not_retried_every_step():
    """None of the dismiss keys exist → the hide fails; retrying it each step cost ~6s
    a step for nothing."""
    class StubbornKb(FakeDeviceDriver):
        def __init__(self):
            super().__init__()
            self.hides = 0
        async def hide_keyboard(self):
            self.hides += 1
            return False

    class KeyboardUp(FakePerceiver):
        async def observe(self, driver):
            space = await super().observe(driver)
            space.keyboard = True
            return space

    d = StubbornKb()
    ex = IOSAgentExecutor(driver=d, perceiver=KeyboardUp(), vision=NoVision(),
                          decider=ScriptedDecider([Tap(ref="e1")] * 4 + [Done(passed=False, reason="r")]))
    await ex.execute(ExecutionSpec(instructions="x", max_steps=8), _noop)
    assert d.hides == 1


# ── typed text: secrets never reach logs, stored traces, events, clauses or the verdict ──

class _LoginPerceiver:
    async def observe(self, driver):
        return ActionSpace(elements=[
            Element(ref="e1", type="SecureTextField", label="Password", frame=Frame(0, 0, 10, 10)),
            Element(ref="e2", type="TextField", label="Search", frame=Frame(0, 20, 10, 10)),
            Element(ref="e3", type="StaticText", label="Wireless Mouse", frame=Frame(0, 40, 10, 10)),
            Element(ref="e4", type="Button", label="heart.fill", frame=Frame(0, 60, 10, 10))],
            text="x", screen_w=390, screen_h=844, source_hash="X")


class _TypeDriver(FakeDeviceDriver):
    async def type(self, el, text, clear=False): pass
    async def type_focused(self, text, clear=False): pass


class _NotesDecider:
    """Returns structured turns whose memory repeats the password BEFORE it is typed."""
    total_tokens = 0

    def __init__(self, actions, memory):
        self._a, self._m = list(actions), memory

    async def decide(self, objective, space, history, screenshot):
        from tamam.executors.ios.brain import Turn
        # The last action repeats, so a `done` the stop gate refuses is simply re-sent.
        action = self._a.pop(0) if len(self._a) > 1 else self._a[0]
        return Turn(evaluation_previous="ok", memory=self._m, clause_updates=[], action=action)


async def _run_login(actions, memory="", instructions="Sign in with password hunter2pw, search Mouse"):
    events = []
    async def collect(e): events.append(e)
    ex = IOSAgentExecutor(driver=_TypeDriver(), perceiver=_LoginPerceiver(), vision=NoVision(),
                          decider=_NotesDecider(actions, memory))
    res = await ex.execute(ExecutionSpec(instructions=instructions, max_steps=6), on_event=collect)
    return res, events


async def test_a_password_in_the_test_is_secret_from_step_one():
    # Step 1 only TAPS; its notes already repeat the password. It must not be stored.
    res, events = await _run_login(
        [Tap(ref="e2"), Type(ref="e1", text="hunter2pw"),
         Done(passed=True, reason="signed in with hunter2pw", repro_steps=["type hunter2pw"])],
        memory="next: type password hunter2pw")
    stored = json.dumps([res.usage_extra["step_trace"], [e.model_dump() for e in events],
                         [e.model_dump() for e in res.steps], res.clauses,
                         res.self_verdict.model_dump()], default=str)
    assert "hunter2pw" not in stored


async def test_test_text_typed_into_an_ordinary_field_stays_readable():
    res, events = await _run_login([Type(ref="e2", text="Mouse"), Done(passed=True, reason="ok")])
    rec = res.usage_extra["step_trace"][0]
    assert rec["action"]["text"] == "Mouse"
    assert rec["action"]["typed"] == {"chars": 5, "secure": False, "in_test_text": True}
    assert "Wireless Mouse" in rec["screen"]["anchors"]      # the screen is not mangled
    assert events[0].actions[0]["text"] == "Mouse"


async def test_invented_text_and_refless_typing_are_secret():
    from tamam.executors.core.redact import MARK
    res, events = await _run_login([Type(ref="e2", text="new.user77@x.io"),
                                    Type(ref="", text="otp884422"), Done(passed=True, reason="ok")])
    blob = json.dumps(res.usage_extra["step_trace"]) + json.dumps([e.model_dump() for e in events])
    assert "new.user77@x.io" not in blob and "otp884422" not in blob and MARK in blob
    assert res.usage_extra["step_trace"][1]["action"]["typed"]["secure"] is True


async def test_trace_record_names_its_target_and_screen():
    res, _ = await _run_login([Tap(ref="e3"), Done(passed=True, reason="ok")])
    rec = res.usage_extra["step_trace"][0]
    assert rec["schema"] == 2
    assert rec["target"]["type"] == "StaticText" and rec["target"]["label"] == "Wireless Mouse"
    assert len(rec["target"]["frame_frac"]) == 4
    assert rec["screen"]["tree_hash"] == "X" and "diag" in rec
    assert "heart.fill" not in rec["screen"]["anchors"]      # icon names don't identify a screen


async def test_a_step_that_dies_still_leaves_its_record():
    from tamam.executors.ios.driver import DriverSessionDead

    class DyingDriver(_TypeDriver):
        async def tap(self, el): raise DriverSessionDead("gone")

    ex = IOSAgentExecutor(driver=DyingDriver(), perceiver=_LoginPerceiver(), vision=NoVision(),
                          decider=_NotesDecider([Tap(ref="e2")], ""))
    res = await ex.execute(ExecutionSpec(instructions="x", max_steps=3),
                           on_event=_noop)
    assert res.usage_extra["step_trace"], "the failing step must be recorded"
    assert res.usage_extra["step_trace"][-1]["step"] == 1


async def test_each_step_trace_is_recorded_on_its_span(monkeypatch):
    import tamam.executors.ios.executor as ex_mod
    seen = []
    monkeypatch.setattr(ex_mod, "span_output", lambda v: seen.append(v))
    ex = IOSAgentExecutor(driver=FakeDeviceDriver(), perceiver=FakePerceiver(), vision=NoVision(),
                          decider=ScriptedDecider([Tap(ref="e1"), Done(passed=True, reason="ok")]))
    res = await ex.execute(ExecutionSpec(instructions="x", max_steps=5),
                           on_event=_noop)
    assert [s["step"] for s in seen] == [s["step"] for s in res.usage_extra["step_trace"]] == [1, 2]


async def test_an_off_screen_coordinate_is_refused_not_clamped_to_the_edge():
    """y=1.98 (a misread) used to be clamped to the bottom edge — a tap on the tab bar.
    It is now refused with a note telling the agent how to read the grid."""
    from tamam.executors.ios.brain import Done

    class RecordingDriver(FakeDeviceDriver):
        def __init__(self):
            super().__init__()
            self.points = []
        async def tap_point(self, x, y):
            self.points.append((x, y))

    drv = RecordingDriver()
    dec = ScriptedDecider([TapPoint(x=0.4, y=1.98, reason="pay button"), Done(passed=False, reason="r")])
    ex = IOSAgentExecutor(driver=drv, perceiver=FakePerceiver(), vision=NoVision(), decider=dec)
    result = await ex.execute(
        ExecutionSpec(instructions="x", max_steps=5), on_event=_noop)
    assert drv.points == []                 # nothing tapped
    assert result.self_verdict is not None  # the run carried on to a verdict


async def test_a_tap_on_an_element_under_the_keyboard_hides_it_instead_of_typing_a_key():
    """the harness never taps through the keyboard; it hides it and says so."""
    from tamam.executors.ios.brain import Done
    from tamam.executors.core.contracts import ActionSpace, Element, Frame
    from tamam.executors.ios.perceiver import KEYBOARD

    class KbPerceiver:
        async def observe(self, driver):
            return ActionSpace(elements=[Element(ref="e1", type="Button", label="Save address",
                                                 frame=Frame(16, 700, 370, 44), covered_by=KEYBOARD)],
                               text='[e1] Button "Save address"', screen_w=402, screen_h=874,
                               source_hash="k", keyboard=True)

    class Driver(FakeDeviceDriver):
        def __init__(self):
            super().__init__()
            self.hidden = 0
        async def hide_keyboard(self):
            self.hidden += 1
            return True

    drv = Driver()
    ex = IOSAgentExecutor(driver=drv, perceiver=KbPerceiver(), vision=NoVision(),
                          decider=ScriptedDecider([Tap(ref="e1"), Done(passed=False, reason="r")]))
    await ex.execute(ExecutionSpec(instructions="x", max_steps=4),
                     on_event=_noop)
    assert ("tap", "e1") not in drv.actions and drv.hidden >= 1


async def test_long_press_and_double_tap_reach_the_driver():
    """gestures beyond a tap — a context menu, a 'hold to confirm' button."""
    from tamam.executors.ios.brain import Done, DoubleTap, LongPress

    class GestureDriver(FakeDeviceDriver):
        async def long_press(self, el, seconds=1.0):
            self.actions.append(("long_press", el.ref, seconds))
        async def double_tap(self, el):
            self.actions.append(("double_tap", el.ref))

    drv = GestureDriver()
    dec = ScriptedDecider([LongPress(ref="e1", seconds=9), DoubleTap(ref="e1"), Done(passed=False, reason="r")])
    ex = IOSAgentExecutor(driver=drv, perceiver=FakePerceiver(), vision=NoVision(), decider=dec)
    await ex.execute(ExecutionSpec(instructions="x", max_steps=5), on_event=_noop)
    assert ("long_press", "e1", 5.0) in drv.actions          # capped at 5 s
    assert ("double_tap", "e1") in drv.actions


async def test_scroll_to_swipes_until_the_text_appears_without_a_decision_per_swipe():
    """one action reaches an item far down a list; it stops at the end of the list."""
    from tamam.executors.ios.brain import Done, ScrollTo
    from tamam.executors.core.contracts import ActionSpace, Element, Frame

    class ListPerceiver:
        def __init__(self):
            self.top = 0
        async def observe(self, driver):
            rows = [f"Row {i}" for i in range(self.top, min(self.top + 10, 40))]
            return ActionSpace(elements=[Element(ref=f"e{i+1}", type="StaticText", label=r,
                                                 frame=Frame(0, 100 + 50 * i, 300, 40))
                                         for i, r in enumerate(rows)],
                               text="", screen_w=390, screen_h=844, source_hash=str(self.top))

    per = ListPerceiver()

    class Driver(FakeDeviceDriver):
        async def swipe(self, d, el=None):
            self.actions.append(("swipe", d))
            per.top = min(per.top + 10, 30)

    drv = Driver()
    dec = ScriptedDecider([ScrollTo(text="Row 35"), ScrollTo(text="Row 99"), Done(passed=False, reason="r")])
    ex = IOSAgentExecutor(driver=drv, perceiver=per, vision=NoVision(), decider=dec)
    result = await ex.execute(ExecutionSpec(instructions="x", max_steps=5),
                              on_event=_noop)
    swipes = [a for a in drv.actions if a[0] == "swipe"]
    # 3 to reach Row 35; Row 99 starts at the end, never moves, so it turns and searches the
    # other way once (3 + 3 more) before reporting the end.
    assert 3 <= len(swipes) <= 3 + 3 + 3
    assert len(result.steps) <= 3             # each scroll_to is ONE step


async def test_drag_moves_an_element_onto_another_and_refuses_an_off_screen_target():
    """drag-and-drop; a bad target is refused, not clamped."""
    from tamam.executors.ios.brain import Done, Drag

    class DragDriver(FakeDeviceDriver):
        async def drag(self, frm, to=None, to_xy=None):
            self.actions.append(("drag", frm.ref, to.ref if to else to_xy))

    drv = DragDriver()
    dec = ScriptedDecider([Drag(ref="e1", to_ref="e2"), Drag(ref="e1", to_x=1.5, to_y=0.5),
                           Done(passed=False, reason="r")])
    ex = IOSAgentExecutor(driver=drv, perceiver=FakePerceiver(), vision=NoVision(), decider=dec)
    await ex.execute(ExecutionSpec(instructions="x", max_steps=5), on_event=_noop)
    assert [a for a in drv.actions if a[0] == "drag"] == [("drag", "e1", "e2")]


async def test_settle_returns_as_soon_as_the_screen_is_still_and_never_waits_past_the_cap(monkeypatch):
    """the flat 1 s pause after every action was 11% of all step time."""
    import time as _t

    from tamam.executors.ios import executor as ex_mod
    monkeypatch.setattr(ex_mod, "_SETTLE_S", 1.0)

    class Still:
        remote = False
        async def screenshot_bytes(self):
            return b"same"

    class Busy:
        remote = False
        n = 0
        async def screenshot_bytes(self):
            Busy.n += 1
            return str(Busy.n).encode()          # a spinner: never two equal frames

    class Remote:
        remote = True
        async def screenshot_bytes(self):
            raise AssertionError("no screenshots over the network for settling")

    t0 = _t.monotonic(); await ex_mod._settle(Still()); still = _t.monotonic() - t0
    t0 = _t.monotonic(); await ex_mod._settle(Busy()); busy = _t.monotonic() - t0
    t0 = _t.monotonic(); await ex_mod._settle(Remote()); remote = _t.monotonic() - t0
    assert still < 0.6 and 0.9 <= busy < 1.4 and 0.9 <= remote < 1.2


async def test_scroll_to_ignores_an_instruction_that_mentions_the_item_and_survives_a_slow_list(monkeypatch):
    """'Scroll to Item 70 and tap it' made scroll_to stop at once; a list that re-renders
    late made it call the end after 2 swipes."""
    from tamam.executors.ios import session as sess_mod
    from tamam.executors.ios.brain import Done, ScrollTo
    from tamam.executors.core.contracts import ActionSpace, Element, Frame
    monkeypatch.setattr(sess_mod, "SCROLL_SETTLE_S", 0)

    class LazyList:
        def __init__(self):
            self.top, self.pending, self.lag = 0, 0, 0
        async def observe(self, driver):
            if self.lag:                     # the first read after a swipe still shows old rows
                self.lag -= 1
            else:
                self.top = self.pending
            rows = [f"Row {i:04d}" for i in range(self.top, self.top + 12)]
            els = [Element(ref="e0", type="StaticText", label="Scroll to Row 0050 and tap it",
                           frame=Frame(0, 90, 390, 30))]
            els += [Element(ref=f"e{i+1}", type="Button", label=r, frame=Frame(0, 130 + 50 * i, 390, 40))
                    for i, r in enumerate(rows)]
            return ActionSpace(elements=els, text="", screen_w=390, screen_h=844, source_hash=str(self.top))

    per = LazyList()

    class Driver(FakeDeviceDriver):
        n = 0
        async def swipe(self, d, el=None):
            self.actions.append(("swipe", d))
            Driver.n += 1
            per.pending = per.pending + 12
            per.lag = 1 if Driver.n % 2 else 0     # every other read is stale: the list re-renders late

    drv = Driver()
    ex = IOSAgentExecutor(driver=drv, perceiver=per, vision=NoVision(),
                          decider=ScriptedDecider([ScrollTo(text="Row 0050"), Done(passed=False, reason="r")]))
    await ex.execute(ExecutionSpec(instructions="x", max_steps=3), on_event=_noop)
    assert per.top >= 48                    # it really scrolled to the row, through the lag



async def test_scroll_to_the_wrong_way_from_the_top_turns_around(monkeypatch):
    """Batch-4 gate, infinite-catalog: the agent asked scroll_to "down" (which swipes toward the
    TOP) on a list already at its top; nothing moved, it was told 'the end of the list', and it
    failed a correct build. A list that never moved was at that end — search the other way."""
    from tamam.executors.ios import session as sess_mod
    from tamam.executors.ios.brain import Done, ScrollTo
    from tamam.executors.core.contracts import ActionSpace, Element, Frame
    monkeypatch.setattr(sess_mod, "SCROLL_SETTLE_S", 0)

    class Catalog:
        top = 0
        async def observe(self, driver):
            rows = [f"Catalog item {i}" for i in range(self.top, self.top + 12)]
            if self.top >= 84:
                rows.append("Golden Ticket")
            els = [Element(ref=f"e{i+1}", type="Button", label=r, frame=Frame(0, 130 + 50 * i, 390, 40))
                   for i, r in enumerate(rows)]
            return ActionSpace(elements=els, text="", screen_w=390, screen_h=844, source_hash=str(self.top))

    per = Catalog()
    first = per.observe

    async def observe_with_ocr_extra(driver):
        # The screen the agent acted on also carries an OCR-added target that later reads lack
        # (live: "Shop Offers Cart More") — that difference must not count as the list moving.
        sp = await first(driver)
        if not getattr(per, "seen", False):
            per.seen = True
            sp.elements.append(Element(ref="e90", type="StaticText", label="Shop Offers Cart More",
                                       frame=Frame(0, 800, 390, 40)))
        return sp
    per.observe = observe_with_ocr_extra

    class Driver(FakeDeviceDriver):
        async def swipe(self, d, el=None):
            self.actions.append(("swipe", d))
            if d == "up":                       # swipe up = further down the list
                per.top += 12
            elif d == "down":
                per.top = max(0, per.top - 12)  # already at the top: nothing moves

    drv = Driver()
    ex = IOSAgentExecutor(driver=drv, perceiver=per, vision=NoVision(),
                          decider=ScriptedDecider([ScrollTo(text="Golden Ticket", direction="down"),
                                                   Done(passed=False, reason="r")]))
    await ex.execute(ExecutionSpec(instructions="x", max_steps=3), on_event=_noop)
    assert per.top >= 84, drv.actions
    assert [a for a in drv.actions if a[0] == "swipe"][:3] == [("swipe", "down")] * 3


async def test_scroll_to_searches_the_other_way_once_after_reaching_an_end(monkeypatch):
    """Batch-6 gate: Shoply's catalog opened part-way down; scroll_to "down" reached the top and
    reported the end, and the agent failed a correct app. An end without the item means one
    search the other way, within the same budget — then the end is reported."""
    from tamam.executors.ios import session as sess_mod
    from tamam.executors.ios.brain import Done, ScrollTo
    from tamam.executors.core.contracts import ActionSpace, Element, Frame
    monkeypatch.setattr(sess_mod, "SCROLL_SETTLE_S", 0)

    class Short:
        top = 0
        async def observe(self, driver):
            els = [Element(ref=f"e{i+1}", type="Button", label=f"Item {i}", frame=Frame(0, 130 + 50 * (i - self.top), 390, 40))
                   for i in range(self.top, self.top + 12)]
            return ActionSpace(elements=els, text="", screen_w=390, screen_h=844, source_hash=str(self.top))

    per = Short()

    class Driver(FakeDeviceDriver):
        async def swipe(self, d, el=None):
            self.actions.append(("swipe", d))
            if d == "up":
                per.top = min(24, per.top + 12)

    drv = Driver()
    ex = IOSAgentExecutor(driver=drv, perceiver=per, vision=NoVision(),
                          decider=ScriptedDecider([ScrollTo(text="Nowhere", direction="up"),
                                                   Done(passed=False, reason="r")]))
    await ex.execute(ExecutionSpec(instructions="x", max_steps=3), on_event=_noop)
    swipes = [a[1] for a in drv.actions if a[0] == "swipe"]
    assert swipes[0] == "up" and "down" in swipes and len(swipes) <= 15
    assert swipes.index("down") > swipes.index("up")       # the far end first, then back

def test_the_turn_asks_for_terse_notes():
    """output tokens are decide time (2,187 traced steps: the longest quarter of turns
    took 4.6 s to decide, the shortest 2.6 s). Notes keep values to compare, not the plan —
    the checklist already tracks what is done, and refs change every screen."""
    from tamam.executors.ios.brain import SYSTEM, Turn
    assert "<= 30 words" in Turn.model_fields["memory"].description
    assert "<= 12 words" in Turn.model_fields["evaluation_previous"].description
    assert "element refs change every screen" in SYSTEM


def _kb_screen(*els, keyboard):
    from tamam.executors.core.contracts import ActionSpace
    keys = []
    if keyboard:
        from tamam.executors.core.contracts import Element, Frame
        keys = [Element(ref="k1", type="Button", label="shift", frame=Frame(0, 700, 60, 40))]
    return ActionSpace(elements=[*els, *keys], text="", screen_w=390, screen_h=844, keyboard=keyboard)


async def _tap_with_keyboard_up(screens, target_label):
    """Tap `target_label` on screens[0] (keyboard up); the device then serves screens[1:]."""
    from tamam.executors.ios import session as sess_mod
    from tamam.executors.ios.brain import Tap
    from tamam.executors.ios.session import DeviceSession

    class Per:
        def __init__(self): self.reads = list(screens[1:])
        async def observe(self, driver):
            return self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]

    class Drv(FakeDeviceDriver):
        async def tap(self, el):
            self.actions.append(("tap", el.label))

    saved, sess_mod.KEYBOARD_CLOSE_S = sess_mod.KEYBOARD_CLOSE_S, 0
    try:
        drv = Drv()
        s = DeviceSession(drv, Per(), reconnect=lambda: None)
        el = next(e for e in screens[0].elements if e.label == target_label)
        note = await s.apply(screens[0], Tap(ref=el.ref))
    finally:
        sess_mod.KEYBOARD_CLOSE_S = saved
    return note, [a for a in drv.actions if a[0] == "tap"]


async def test_a_tap_that_only_closed_the_keyboard_is_tapped_again():
    """a React Native search with a debounce: the first tap on a search result only closed the
    keyboard; the agent tapped once and failed a correct app. Probe: 0/3 on one tap, 2/2 on two."""
    from tamam.executors.core.contracts import Element, Frame
    field = Element(ref="e1", type="TextField", label="search-input", value="Wireless Mouse", frame=Frame(0, 100, 390, 40))
    row = Element(ref="e2", type="Other", label="Wireless Mouse", frame=Frame(0, 160, 390, 40))
    note, taps = await _tap_with_keyboard_up(
        [_kb_screen(field, row, keyboard=True), _kb_screen(field, row, keyboard=False)], "Wireless Mouse")
    assert taps == [("tap", "Wireless Mouse")] * 2
    assert "only closed the keyboard" in note


async def test_a_tap_with_a_real_effect_is_never_tapped_twice():
    """A toggle that flipped (or anything that appeared) is a real effect: a second tap would undo it."""
    from tamam.executors.core.contracts import Element, Frame
    off = Element(ref="e2", type="Switch", label="Rush delivery", toggled=False, frame=Frame(0, 160, 390, 40))
    on = Element(ref="e2", type="Switch", label="Rush delivery", toggled=True, frame=Frame(0, 160, 390, 40))
    note, taps = await _tap_with_keyboard_up(
        [_kb_screen(off, keyboard=True), _kb_screen(on, keyboard=False)], "Rush delivery")
    assert len(taps) == 1 and "only closed" not in note
    row = Element(ref="e2", type="Other", label="Wireless Mouse", frame=Frame(0, 160, 390, 40))
    done = Element(ref="e3", type="StaticText", label="Selected Wireless Mouse", frame=Frame(0, 220, 390, 40))
    note, taps = await _tap_with_keyboard_up(
        [_kb_screen(row, keyboard=True), _kb_screen(row, done, keyboard=False)], "Wireless Mouse")
    assert len(taps) == 1


async def test_no_keyboard_no_second_look():
    from tamam.executors.core.contracts import Element, Frame
    row = Element(ref="e2", type="Other", label="Wireless Mouse", frame=Frame(0, 160, 390, 40))
    note, taps = await _tap_with_keyboard_up(
        [_kb_screen(row, keyboard=False), _kb_screen(row, keyboard=False)], "Wireless Mouse")
    assert len(taps) == 1


async def test_scroll_to_keeps_going_when_the_tree_goes_blind_but_the_pixels_move(monkeypatch):
    """an RN list of 500 rows: past a list's 100th child the rows leave the accessibility
    tree (snapshotMaxChildren) while the screen keeps scrolling. The tree stopped changing at
    Row 0099, scroll_to called it the end, turned around, and the agent failed a correct app.
    When the tree is unchanged, the screen's own text decides: moving pixels are not an end."""
    from tamam.executors.ios import session as sess_mod
    from tamam.executors.ios.brain import ScrollTo
    from tamam.executors.core.contracts import ActionSpace, Element, Frame
    monkeypatch.setattr(sess_mod, "SCROLL_SETTLE_S", 0)

    class Blind:
        top = 0
        async def observe(self, driver):
            rows = [f"Row {i:04d}" for i in range(self.top, self.top + 12) if i < 100]
            els = [Element(ref=f"e{i+1}", type="StaticText", label=r, frame=Frame(0, 130 + 50 * i, 390, 40))
                   for i, r in enumerate(rows)]
            return ActionSpace(elements=els, text="", screen_w=390, screen_h=844,
                               source_hash="|".join(rows) or "empty")

    per = Blind()
    monkeypatch.setattr(sess_mod, "ocr_lines",
                        lambda path: [f"Row {i:04d}" for i in range(per.top, per.top + 12)])

    class Driver(FakeDeviceDriver):
        async def swipe(self, d, el=None):
            self.actions.append(("swipe", d))
            if d == "up":
                per.top = min(per.top + 12, 588)
        async def screenshot(self):
            return "/tmp/x.png"

    from tamam.executors.ios.session import DeviceSession
    drv = Driver()
    s = DeviceSession(drv, per, reconnect=lambda: None)
    first = await s.apply(await per.observe(drv), ScrollTo(text="Row 0120"))
    second = await s.apply(await per.observe(drv), ScrollTo(text="Row 0200"))
    assert '"Row 0120" appeared on screen' in first and '"Row 0200" appeared on screen' in second
    assert {a[1] for a in drv.actions if a[0] == "swipe"} == {"up"}      # never turned around


def test_ocr_noise_on_a_still_screen_is_not_a_scroll():
    from tamam.executors.ios.session import _scrolled
    still = {"All 600 rows are loaded.", "Row 0000", "Row 0001", "Row 0002", "Row 0003"}
    noisy = {"All 600 rows are loaded", "Row 0000", "Row 0001", "Row 0002", "Row 0003"}
    moved = {"Row 0012", "Row 0013", "Row 0014", "Row 0015", "Row 0016"}
    assert not _scrolled(still, noisy) and _scrolled(still, moved)


async def _hide(screens, monkeypatch):
    from tamam.executors.ios import session as sess_mod
    from tamam.executors.ios.brain import HideKeyboard
    from tamam.executors.ios.session import DeviceSession
    monkeypatch.setattr(sess_mod, "KEYBOARD_CLOSE_S", 0)

    class Per:
        def __init__(self): self.reads = list(screens[1:])      # screens[0] is the one acted on
        async def observe(self, driver):
            return self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]

    class Drv(FakeDeviceDriver):
        async def hide_keyboard(self):
            self.actions.append(("hide",)); return True
        async def tap(self, el):
            self.actions.append(("tap", el.label))

    drv = Drv()
    note = await DeviceSession(drv, Per(), reconnect=lambda: None).apply(screens[0], HideKeyboard())
    return note, drv.actions


async def test_a_keyboard_without_a_done_key_is_moved_by_tapping_plain_text(monkeypatch):
    """a sign-up form: hideKeyboard 'succeeded' 8 times while the keyboard stayed up. The
    harness now checks, then taps a heading (the agent's own working fix)."""
    from tamam.executors.core.contracts import Element, Frame
    nav = Element(ref="e0", type="StaticText", label="Profile Setup", frame=Frame(120, 60, 160, 30))
    title = Element(ref="e1", type="StaticText", label="A few details about you", frame=Frame(20, 120, 300, 30))
    field = Element(ref="e2", type="TextField", label="name-input", frame=Frame(20, 200, 300, 40))
    up = _kb_screen(nav, title, field, keyboard=True)
    down = _kb_screen(nav, title, field, keyboard=False)
    note, acts = await _hide([up, up, down], monkeypatch)
    assert acts == [("hide",), ("tap", "A few details about you")] and "tapped the plain text" in note


async def test_a_keyboard_that_hid_is_not_followed_by_a_tap(monkeypatch):
    from tamam.executors.core.contracts import Element, Frame
    title = Element(ref="e1", type="StaticText", label="A few details about you", frame=Frame(20, 120, 300, 30))
    nav = Element(ref="e0", type="StaticText", label="Profile Setup", frame=Frame(120, 60, 160, 30))
    note, acts = await _hide([_kb_screen(nav, title, keyboard=True), _kb_screen(nav, title, keyboard=False)], monkeypatch)
    assert acts == [("hide",)] and note.endswith("hid the keyboard")


async def test_a_coordinate_tap_on_the_keyboard_is_refused_and_the_keyboard_put_away(monkeypatch):
    """a flattened form: a tap_point at y=0.76 landed on the keyboard and typed a 'g' into
    the name field; the agent then failed a correct app for 'corrupted' input."""
    from tamam.executors.ios import session as sess_mod
    from tamam.executors.ios.brain import TapPoint
    from tamam.executors.core.contracts import ActionSpace
    from tamam.executors.ios.session import DeviceSession
    monkeypatch.setattr(sess_mod, "KEYBOARD_CLOSE_S", 0)
    up = ActionSpace(elements=[], text="", screen_w=400, screen_h=800, keyboard=True, keyboard_top=540)
    down = ActionSpace(elements=[], text="", screen_w=400, screen_h=800, keyboard=False)

    class Per:
        async def observe(self, driver): return down

    class Drv(FakeDeviceDriver):
        async def tap_point(self, x, y): self.actions.append(("tap_point", x, y))
        async def hide_keyboard(self): self.actions.append(("hide",)); return True

    drv = Drv()
    s = DeviceSession(drv, Per(), reconnect=lambda: None)
    note = await s.apply(up, TapPoint(x=0.5, y=0.76))
    assert ("hide",) in drv.actions and not any(a[0] == "tap_point" for a in drv.actions)
    assert "on the keyboard" in note
    note = await s.apply(up, TapPoint(x=0.5, y=0.30))           # above the keyboard: performed
    assert ("tap_point", 200, 240) in drv.actions


def test_garbled_typing_is_the_agents_input_not_an_app_failure():
    """two flattened RN forms were FAILED over a field showing 'adag' — text the
    harness typed. The agent is told that is its own input: retype, then blocked, never failed."""
    from tamam.executors.ios.brain import SYSTEM
    assert "your typing did not land as typed" in SYSTEM and "never an app bug" in SYSTEM



async def _tap_toggle_row(flips_at, monkeypatch):
    """A wide 'I agree' row (370x64) whose value flips only when tapped at `flips_at`."""
    from tamam.executors.ios import session as sess_mod
    from tamam.executors.ios.brain import Tap
    from tamam.executors.core.contracts import ActionSpace, Element, Frame
    from tamam.executors.ios.session import DeviceSession
    monkeypatch.setattr(sess_mod, "KEYBOARD_CLOSE_S", 0)
    state = {"on": False}

    def row():
        return Element(ref="e6", type="Switch", label="I agree to the terms of service.",
                       frame=Frame(16, 294, 370, 64), toggled=state["on"])

    class Per:
        async def observe(self, driver):
            return ActionSpace(elements=[row()], text="", screen_w=402, screen_h=874)

    class Drv(FakeDeviceDriver):
        async def tap(self, el):
            self.actions.append(("tap", "centre")); state["on"] ^= flips_at == "centre"
        async def tap_point(self, x, y):
            side = "left" if x < 201 else "right"
            self.actions.append(("tap_point", side)); state["on"] ^= flips_at == side

    drv = Drv()
    note = await DeviceSession(drv, Per(), reconnect=lambda: None).apply(
        ActionSpace(elements=[row()], text="", screen_w=402, screen_h=874), Tap(ref="e6"))
    return note, drv.actions, state["on"]


async def test_a_merged_checkbox_row_is_tapped_at_its_control_when_the_centre_does_nothing(monkeypatch):
    """Flutter: the terms checkbox and its label are one 370x64 Switch; the centre is the
    label and does nothing (probe: centre 0/3, right edge no, left edge yes). The agent tapped
    the centre 10 times and failed a correct app."""
    note, acts, on = await _tap_toggle_row("left", monkeypatch)
    assert on and acts == [("tap", "centre"), ("tap_point", "right"), ("tap_point", "left")]
    assert "left edge" in note


async def test_a_row_that_flips_on_a_centre_tap_gets_no_extra_taps(monkeypatch):
    note, acts, on = await _tap_toggle_row("centre", monkeypatch)
    assert on and acts == [("tap", "centre")]          # never flipped twice


async def test_scroll_to_turns_around_even_while_the_list_keeps_loading(monkeypatch):
    """Batch-6 gate, infinite-catalog: at the top, scrolled the wrong way, but the catalog kept
    loading ('Loaded: 40 items' -> 60 -> 100) so the screen kept changing and scroll_to never
    turned around. Movement is rows shifting, not text changing."""
    from tamam.executors.ios import session as sess_mod
    from tamam.executors.ios.brain import ScrollTo
    from tamam.executors.core.contracts import ActionSpace, Element, Frame
    from tamam.executors.ios.session import DeviceSession
    monkeypatch.setattr(sess_mod, "SCROLL_SETTLE_S", 0)

    class Loading:
        top, loaded = 0, 20
        async def observe(self, driver):
            self.loaded = min(self.loaded + 20, 100)
            rows = [Element(ref="e0", type="StaticText", label=f"Loaded: {self.loaded} items", frame=Frame(0, 100, 390, 30))]
            rows += [Element(ref=f"e{i+1}", type="StaticText", label=f"Catalog item {self.top + i}",
                             frame=Frame(0, 140 + 50 * i, 390, 40)) for i in range(12)]
            if self.top >= 84:
                rows.append(Element(ref="e99", type="StaticText", label="Golden Ticket — $5", frame=Frame(0, 760, 390, 40)))
            return ActionSpace(elements=rows, text="", screen_w=390, screen_h=844, source_hash=str((self.top, self.loaded)))

    per = Loading()

    class Driver(FakeDeviceDriver):
        async def swipe(self, d, el=None):
            self.actions.append(("swipe", d))
            if d == "up":
                per.top += 12

    drv = Driver()
    s = DeviceSession(drv, per, reconnect=lambda: None)
    note = await s.apply(await per.observe(drv), ScrollTo(text="Golden Ticket", direction="down"))
    assert "turned up" in note and "appeared" in note, note


async def test_typing_into_a_label_types_into_the_box_under_it(monkeypatch):
    """a flattened form: the agent sees the label 'First name' (OCR) but no field; typing into
    the label taps the box found under it, then types."""
    from tamam.executors.ios import session as sess_mod
    from tamam.executors.ios.brain import Type
    from tamam.executors.core.contracts import ActionSpace, Element, Frame
    from tamam.executors.ios.session import DeviceSession
    monkeypatch.setattr(sess_mod, "KEYBOARD_CLOSE_S", 0)
    monkeypatch.setattr(sess_mod, "input_box_below", lambda png, frame, w: Frame(16, 203, 370, 44))

    class Drv(FakeDeviceDriver):
        async def tap_point(self, x, y): self.actions.append(("tap_point", x, y))
        async def type_focused(self, text, clear=False): self.actions.append(("type_focused", text))

    drv = Drv()
    label = Element(ref="e5", type="Image", label="First name", frame=Frame(24, 183, 90, 12))
    space = ActionSpace(elements=[label], text="", screen_w=402, screen_h=874)
    note = await DeviceSession(drv, FakePerceiver(), reconnect=lambda: None).apply(space, Type(ref="e5", text="Ada"))
    assert drv.actions[-2:] == [("tap_point", 201, 225), ("type_focused", "Ada")]
    assert 'field under "First name"' in note


def test_a_named_gesture_that_does_nothing_is_an_app_failure_not_blocked():
    """the agent long-pressed 'Hold to redeem' 4 times (the planted bug: it does nothing)
    and reported blocked=cannot_reach — a missed catch. Reaching a control by its ref is never
    cannot_reach; a gesture the test names that does nothing twice is the app's failure."""
    from tamam.executors.ios.brain import SYSTEM
    assert "long-press" in SYSTEM.split("NOT blocked")[1][:400]
    assert "never cannot_reach" in SYSTEM


def test_the_agent_is_told_tabs_live_on_root_screens():
    """Wikipedia: the article page hides the tab bar; the agent hunted through Tabs and
    search overlays for 'Saved' for 20 steps. Generic: go back until the tab bar shows."""
    from tamam.executors.ios.brain import SYSTEM
    assert "go back until the tab bar shows" in SYSTEM


async def test_typing_a_password_under_its_label_warns_the_screenshot_hides_it(monkeypatch):
    """a flattened form: iOS keeps a password field's text out of screenshots; the agent saw
    an empty box after typing and failed a correct app."""
    from tamam.executors.ios import session as sess_mod
    from tamam.executors.ios.brain import Type
    from tamam.executors.core.contracts import ActionSpace, Element, Frame
    from tamam.executors.ios.session import DeviceSession
    monkeypatch.setattr(sess_mod, "KEYBOARD_CLOSE_S", 0)
    monkeypatch.setattr(sess_mod, "input_box_below", lambda png, frame, w: Frame(16, 456, 370, 44))

    class Drv(FakeDeviceDriver):
        async def tap_point(self, x, y): pass
        async def type_focused(self, text, clear=False): pass
        async def hide_keyboard(self): self.actions.append(("hide",)); return True

    drv = Drv()
    s = DeviceSession(drv, FakePerceiver(), reconnect=lambda: None)
    for label, warned in (("Set password", True), ("Email address", False)):
        el = Element(ref="e5", type="Image", label=label, frame=Frame(24, 439, 90, 12))
        note = await s.apply(ActionSpace(elements=[el], text="", screen_w=402, screen_h=874), Type(ref="e5", text="x"))
        assert ("screenshots never show a password field" in note) == warned, label
    # After the password, the keyboard is put away (screenshots hide it, so they'd lie about
    # what it covers); not after an ordinary field.
    assert drv.actions.count(("hide",)) == 1


async def test_a_keyboard_that_vanishes_for_one_read_is_not_called_hidden(monkeypatch):
    """one read caught the keyboard mid-animation, the note said 'hid the keyboard', and it
    was still up on the next step. Hidden means gone on two reads in a row."""
    from tamam.executors.ios import session as sess_mod
    from tamam.executors.core.contracts import Element, Frame
    monkeypatch.setattr(sess_mod, "KEYBOARD_RECHECK_S", 0)
    title = Element(ref="e1", type="StaticText", label="Step 1 of 3", frame=Frame(20, 140, 300, 30))
    up, down = _kb_screen(title, keyboard=True), _kb_screen(title, keyboard=False)
    # acted-on screen, then: gone (blip), back, [tap the text], gone, gone
    note, acts = await _hide([up, down, up, down, down], monkeypatch)
    assert acts == [("hide",), ("tap", "Step 1 of 3")] and "tapped the plain text" in note


async def test_a_credential_keyboard_that_will_not_hide_is_closed_with_return(monkeypatch):
    """a flattened sign-in form: no Done key, taps on the screen do not move it, and the agent never
    pressed return. iOS's 'Passwords' AutoFill key marks a one-line credential field, where return
    cannot add a newline: the harness presses it. Without that key, it never does."""
    from tamam.executors.ios import session as sess_mod
    from tamam.executors.ios.brain import HideKeyboard
    from tamam.executors.core.contracts import ActionSpace, Element, Frame
    from tamam.executors.ios.session import DeviceSession
    monkeypatch.setattr(sess_mod, "KEYBOARD_CLOSE_S", 0)
    monkeypatch.setattr(sess_mod, "KEYBOARD_RECHECK_S", 0)

    def screen(kb, autofill):
        els = [Element(ref="e1", type="Button", label="Sample Screens", frame=Frame(16, 60, 200, 44))]
        if kb:
            els += [Element(ref="e4", type="Button", label="return", frame=Frame(300, 780, 90, 40))]
            if autofill:
                els += [Element(ref="e3", type="Button", label="Passwords", frame=Frame(150, 540, 100, 36))]
        return ActionSpace(elements=els, text="", screen_w=402, screen_h=874, keyboard=kb,
                           keyboard_top=583 if kb else 0)

    for autofill in (True, False):
        state = {"kb": True}

        class Per:
            async def observe(self, driver): return screen(state["kb"], autofill)

        class Drv(FakeDeviceDriver):
            async def hide_keyboard(self): return False
            async def tap(self, el):
                self.actions.append(("tap", el.label))
                if el.label == "return":
                    state["kb"] = False

        drv = Drv()
        note = await DeviceSession(drv, Per(), reconnect=lambda: None).apply(screen(True, autofill), HideKeyboard())
        pressed = ("tap", "return") in drv.actions
        assert pressed == autofill, autofill
        assert ("pressing its return key" in note) == autofill


async def test_a_low_coordinate_tap_rereads_a_keyboard_the_step_missed(monkeypatch):
    """right after typing, the step's read said 'no keyboard'; the tap typed a letter into the
    password. A coordinate tap where a keyboard could be reads the screen again first."""
    from tamam.executors.ios import session as sess_mod
    from tamam.executors.ios.brain import TapPoint
    from tamam.executors.core.contracts import ActionSpace
    from tamam.executors.ios.session import DeviceSession
    monkeypatch.setattr(sess_mod, "KEYBOARD_CLOSE_S", 0)
    monkeypatch.setattr(sess_mod, "KEYBOARD_RECHECK_S", 0)
    stale = ActionSpace(elements=[], text="", screen_w=402, screen_h=874, keyboard=False)
    real = ActionSpace(elements=[], text="", screen_w=402, screen_h=874, keyboard=True, keyboard_top=583)

    class Per:
        async def observe(self, driver): return real

    class Drv(FakeDeviceDriver):
        async def tap_point(self, x, y): self.actions.append(("tap_point", x, y))
        async def hide_keyboard(self): return False

    drv = Drv()
    note = await DeviceSession(drv, Per(), reconnect=lambda: None).apply(stale, TapPoint(x=0.5, y=0.68))
    assert not any(a[0] == "tap_point" for a in drv.actions) and "on the keyboard" in note
