"""Alert policy: the app's own dialogs are the agent's to see; iOS system dialogs are
accepted by the harness and reported. WebDriverAgent's autoAcceptAlerts (which accepted
BOTH, blindly, every 2s) is off by default — on the Shoply fixture it dismissed a
"Couldn't save your changes" error before anyone saw it (defect #15)."""

import pytest

from tamam.config import get_settings
from tamam.executors.base import ExecutionSpec
from tamam.executors.ios.brain import Done
from tamam.executors.ios.driver import AppiumDriver
from tamam.executors.ios.executor import IOSAgentExecutor
from tamam.executors.ios.perceiver import IOSPerceiver, system_dialog_title

# While a system dialog is up, WDA (respectSystemAlerts) returns SpringBoard: blank root.
SYSTEM = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication" name=" " label=" " enabled="true" visible="true" x="0" y="0" width="402" height="874">
  <XCUIElementTypeAlert type="XCUIElementTypeAlert" name="“Shop” Would Like to Send You Notifications" label="“Shop” Would Like to Send You Notifications" enabled="true" visible="true" x="60" y="300" width="280" height="200">
    <XCUIElementTypeButton type="XCUIElementTypeButton" name="Don’t Allow" label="Don’t Allow" enabled="true" visible="true" x="60" y="450" width="140" height="44"/>
    <XCUIElementTypeButton type="XCUIElementTypeButton" name="Allow" label="Allow" enabled="true" visible="true" x="200" y="450" width="140" height="44"/>
  </XCUIElementTypeAlert>
</XCUIElementTypeApplication>"""

APP_ALERT = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication" name="Shop" label="Shop" enabled="true" visible="true" x="0" y="0" width="402" height="874">
  <XCUIElementTypeAlert type="XCUIElementTypeAlert" name="Couldn't save your changes" label="Couldn't save your changes" enabled="true" visible="true" x="60" y="300" width="280" height="200">
    <XCUIElementTypeButton type="XCUIElementTypeButton" name="OK" label="OK" enabled="true" visible="true" x="60" y="450" width="280" height="44"/>
  </XCUIElementTypeAlert>
</XCUIElementTypeApplication>"""

APP = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication" name="Shop" label="Shop" enabled="true" visible="true" x="0" y="0" width="402" height="874">
  <XCUIElementTypeSwitch type="XCUIElementTypeSwitch" name="Order updates" label="Order updates" value="1" enabled="true" visible="true" x="16" y="168" width="370" height="54"/>
</XCUIElementTypeApplication>"""

HOME = """<XCUIElementTypeApplication type="XCUIElementTypeApplication" name=" " label=" " x="0" y="0" width="402" height="874">
  <XCUIElementTypeIcon type="XCUIElementTypeIcon" name="Shop" label="Shop" enabled="true" visible="true" x="20" y="100" width="60" height="60"/>
</XCUIElementTypeApplication>"""


class SourceDriver:
    """Serves page sources in order; records alert accepts."""

    def __init__(self, sources, accept=True):
        self.sources = list(sources)
        self.reads = 0
        self.accepts = 0
        self._accept = accept

    async def accessibility(self):
        self.reads += 1
        return self.sources.pop(0) if len(self.sources) > 1 else self.sources[0]

    def screen_size(self):
        return (402, 874)

    async def dismiss_alert(self):
        self.accepts += 1
        return "accepted" if self._accept else None


def test_system_dialog_is_recognised_only_over_springboard():
    assert system_dialog_title(SYSTEM) == "“Shop” Would Like to Send You Notifications"
    assert system_dialog_title(APP_ALERT) is None      # the app's own dialog: agent's call
    assert system_dialog_title(APP) is None
    assert system_dialog_title(HOME) is None           # SpringBoard without a dialog
    assert system_dialog_title("") is None


async def test_system_dialog_is_accepted_reported_and_the_app_reread():
    d = SourceDriver([SYSTEM, APP])
    space = await IOSPerceiver().observe(d)
    assert d.accepts == 1 and d.reads == 2
    assert [e.label for e in space.elements] == ["Order updates"]
    assert len(space.harness_notes) == 1
    assert "Would Like to Send You Notifications" in space.harness_notes[0]


async def test_app_dialog_is_left_for_the_agent():
    d = SourceDriver([APP_ALERT])
    space = await IOSPerceiver().observe(d)
    assert d.accepts == 0
    assert "OK" in [e.label for e in space.elements]
    assert space.harness_notes == []


async def test_unacceptable_system_dialog_is_shown_not_looped_on():
    d = SourceDriver([SYSTEM], accept=False)
    space = await IOSPerceiver().observe(d)
    assert d.accepts == 1 and d.reads == 1
    assert "Allow" in [e.label for e in space.elements]


async def test_back_to_back_system_dialogs_are_bounded():
    d = SourceDriver([SYSTEM])  # the dialog never goes away
    await IOSPerceiver().observe(d)
    assert d.accepts == IOSPerceiver.MAX_SYSTEM_DIALOGS


async def test_system_dialog_acceptance_can_be_turned_off():
    d = SourceDriver([SYSTEM, APP])
    await IOSPerceiver(accept_system_dialogs=False).observe(d)
    assert d.accepts == 0


async def test_the_accepted_system_dialog_reaches_the_agents_history():
    class Perceiver:
        async def observe(self, driver):
            return await IOSPerceiver().observe(SourceDriver([SYSTEM, APP]))

    from tests.test_ios_executor import FakeDeviceDriver, NoVision

    seen = []

    class Decider:
        total_tokens = 0
        async def decide(self, objective, space, history, screenshot):
            seen.append(list(history))
            return Done(passed=False, reason="x")

    ex = IOSAgentExecutor(driver=FakeDeviceDriver(), perceiver=Perceiver(), vision=NoVision(),
                          decider=Decider())
    async def _noop(e): pass
    await ex.execute(ExecutionSpec(instructions="x", max_steps=3),
                     on_event=_noop)
    assert any("system dialog" in h and "accepted" in h for h in seen[0])


# ── session settings / capabilities ───────────────────────────────────────────


class _WDA:
    capabilities = {}

    def __init__(self, drop=()):
        self.s, self.drop = {}, set(drop)

    def update_settings(self, d):
        self.s.update({k: v for k, v in d.items() if k not in self.drop})

    def get_settings(self):
        return dict(self.s)


@pytest.fixture
def legacy_alerts(monkeypatch):
    monkeypatch.setenv("TAMAM_IOS_AUTO_ACCEPT_ALERTS", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_system_dialogs_are_made_visible_by_default(tmp_path):
    s = _WDA()
    await AppiumDriver(session=s, artifacts_dir=str(tmp_path))._tune_perception()
    assert s.s["respectSystemAlerts"] is True


async def test_a_webdriveragent_without_system_alert_support_is_reported(tmp_path):
    d = AppiumDriver(session=_WDA(drop={"respectSystemAlerts"}), artifacts_dir=str(tmp_path))
    await d._tune_perception()
    assert "respectSystemAlerts" in d.dropped_settings


async def test_legacy_switch_keeps_the_old_behaviour(tmp_path, legacy_alerts):
    s = _WDA()
    await AppiumDriver(session=s, artifacts_dir=str(tmp_path))._tune_perception()
    assert "respectSystemAlerts" not in s.s
    from appium.options.ios import XCUITestOptions

    from tamam.executors.ios.providers.common import apply_alert_policy
    o = XCUITestOptions()
    apply_alert_policy(o)
    assert o.get_capability("autoAcceptAlerts") is True


def test_sessions_no_longer_auto_accept_every_alert():
    from appium.options.ios import XCUITestOptions

    from tamam.executors.ios.providers.common import apply_alert_policy
    o = XCUITestOptions()
    apply_alert_policy(o)
    assert o.get_capability("autoAcceptAlerts") is None


def test_an_app_dialog_title_is_on_the_space():
    from tamam.executors.ios.perceiver import app_dialog_title
    assert app_dialog_title(APP_ALERT) == "Couldn't save your changes"
    assert app_dialog_title(APP) == ""
    assert app_dialog_title(SYSTEM) == ""        # a system dialog is not the app's
