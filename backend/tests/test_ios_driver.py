"""AppiumDriver — screenshot access must be serialized on the shared session.

One Appium session runs one command at a time; the live-preview governor and the
per-step capture both screenshot it. Concurrent calls collide and capture fails.
This asserts the driver's lock serializes them (no overlap).
"""
import asyncio

import pytest

from tamam.executors.ios.driver import AppiumDriver, DriverSessionDead


class OverlapDetectingSession:
    """get_screenshot_as_png raises if a second call starts before the first ends —
    i.e. it detects concurrent (unserialized) access to the session."""

    def __init__(self):
        self.in_progress = False
        self.calls = 0
        self.overlaps = 0

    def get_screenshot_as_png(self):
        if self.in_progress:
            self.overlaps += 1
            raise RuntimeError("concurrent screenshot on one session")
        self.in_progress = True
        import time
        time.sleep(0.02)  # hold the 'session' briefly to expose overlap
        self.in_progress = False
        self.calls += 1
        return b"\x89PNG"


class WedgedSession:
    """A dead/wedged Appium session: quit() and remove_app() block until released. This is
    the exact failure the teardown timeouts must survive — otherwise stop() hangs the run.
    A releasable Event (not a raw sleep) lets the abandoned threads exit at test end so the
    process shuts down cleanly."""

    def __init__(self):
        import threading
        self.quit_started = False
        self._release = threading.Event()

    def quit(self):
        self.quit_started = True
        self._release.wait(timeout=30)  # blocks past the driver's 0.2s teardown budget

    def remove_app(self, bundle):
        self._release.wait(timeout=30)

    def release(self):
        self._release.set()


async def test_stop_is_bounded_when_session_is_wedged(tmp_path):
    sess = WedgedSession()
    d = AppiumDriver(sess, tmp_path, remote=True)
    d._cmd_timeout = 0.2  # tighten the value the driver actually reads (fast, real path)
    d._read_timeout = 0.2
    d.teardown_uninstall = True
    d.teardown_uninstall_bundle = "com.example.app"
    try:
        # Even though quit()/remove_app() block, stop() must return promptly (bounded).
        await asyncio.wait_for(d.stop(), timeout=5)
        assert sess.quit_started is True  # it really tried to quit, then gave up on timeout
    finally:
        sess.release()  # let the abandoned teardown threads exit so the process can too


async def test_screenshot_calls_are_serialized(tmp_path):
    sess = OverlapDetectingSession()
    d = AppiumDriver(sess, tmp_path, remote=True)
    # per-step capture + governor capture firing together, repeatedly
    tasks = []
    for _ in range(5):
        tasks.append(d.screenshot())
        tasks.append(d.screenshot_bytes())
    await asyncio.gather(*tasks)
    assert sess.overlaps == 0          # the lock prevented all overlaps
    assert sess.calls == 10            # every capture still ran


class DeadAtStartSession:
    """A session that's already gone by the first command — the exact AWS Device Farm
    failure: a slow-provisioned device hands back a session that has expired before
    get_window_size, so the first call gets 'Session does not exist'."""

    def get_window_size(self):
        raise Exception("invalid session id: Session does not exist")


async def test_start_surfaces_dead_session_as_recoverable(tmp_path):
    """start()'s FIRST command must raise DriverSessionDead (recoverable via the provider's
    reconnect hook), NOT a raw InvalidSessionIdException that dead-ends the whole run before
    a single step. This is the fix for the '0 steps, errored at getWindowRect' cloud run."""
    d = AppiumDriver(DeadAtStartSession(), tmp_path, remote=True)
    d._cmd_timeout = 1
    with pytest.raises(DriverSessionDead):
        await d.start()


# --- backgrounding returns the app to the foreground ----------------------------

def test_dead_session_detection_covers_a_crashed_app():
    """'not running, possibly crashed' is recoverable (relaunch), not a hard error."""
    from tamam.executors.ios.driver import _looks_like_dead_session
    assert _looks_like_dead_session(
        Exception("The application under test with bundle id 'com.x.y' is not running, "
                  "possibly crashed"))
    assert not _looks_like_dead_session(Exception("element not found"))


# --- simulator crashes are written on the HOST, not the device ------------------

def test_host_crash_reports_finds_this_app_and_explains_the_cause(tmp_path, monkeypatch):
    import json
    import time
    from tamam.executors.ios import driver as drv

    report = {
        "exception": {"type": "EXC_CRASH", "signal": "SIGABRT"},
        "lastExceptionBacktrace": [
            {"symbol": "__exceptionPreprocess"},
            {"symbol": "objc_exception_throw"},
            {"symbol": "-[UITableView _Bug_Detected_In_Client_Of_UITableView_Invalid_Number_Of_Rows_In_Section:]"},
        ],
    }
    mine = tmp_path / "Shoply-2026-09-22-100000.ips"
    mine.write_text(json.dumps({"app_name": "Shoply", "app_version": "1.0",
                                "bundleID": "dev.tamam.shoply"}) + "\n"
                    + json.dumps(report))
    other = tmp_path / "Other-2026-09-22-100000.ips"
    other.write_text(json.dumps({"app_name": "Other", "bundleID": "com.someone.else"}) + "\n"
                     + json.dumps(report))
    monkeypatch.setattr(drv, "HOST_CRASH_DIR", tmp_path)

    found = drv.host_crash_reports("dev.tamam.shoply", since_epoch=time.time() - 60)
    assert len(found) == 1, "another app's crash must never be attributed to this run"
    assert "SIGABRT" in found[0]
    assert "Invalid_Number_Of_Rows_In_Section" in found[0], \
        "the report must name the cause, not just say 'it crashed'"


def test_host_crash_reports_ignores_crashes_from_before_the_run(tmp_path, monkeypatch):
    import json
    import os
    import time
    from tamam.executors.ios import driver as drv

    old = tmp_path / "Shoply-old.ips"
    old.write_text(json.dumps({"app_name": "Shoply", "bundleID": "dev.tamam.shoply"})
                   + "\n" + json.dumps({"exception": {"type": "EXC_CRASH"}}))
    stale = time.time() - 3600
    os.utime(old, (stale, stale))
    monkeypatch.setattr(drv, "HOST_CRASH_DIR", tmp_path)
    assert drv.host_crash_reports("dev.tamam.shoply", since_epoch=time.time() - 60) == []


async def test_hide_keyboard_never_presses_an_action_key(tmp_path):
    """return/Go/Search/Next/Send submit, search, send or insert a newline. The harness
    tidying the keyboard must never take an action the agent did not choose."""
    from tamam.executors.ios.driver import AppiumDriver

    class RecSession:
        capabilities = {}
        def __init__(self): self.calls = []
        def execute_script(self, name, args=None): self.calls.append((name, args))

    sess = RecSession()
    d = AppiumDriver(session=sess, artifacts_dir=str(tmp_path))
    await d.hide_keyboard()
    keys = {k.lower() for k in sess.calls[0][1]["keys"]}
    assert keys and not keys & {"return", "go", "search", "next", "send"}


async def test_dismiss_alert_returns_the_alert_text(tmp_path):
    from tamam.executors.ios.driver import AppiumDriver

    class Alert:
        text = "Allow “Shoply” to send you notifications?"
        def __init__(self): self.accepted = False
        def accept(self): self.accepted = True

    class SwitchTo:
        def __init__(self): self.alert = Alert()

    class Sess:
        capabilities = {}
        def __init__(self): self.switch_to = SwitchTo()

    d = AppiumDriver(session=Sess(), artifacts_dir=str(tmp_path))
    assert "notifications" in await d.dismiss_alert()


async def test_capture_diagnostics_is_bounded_when_session_is_wedged(tmp_path):
    """An unguarded get_log on a wedged session blocked past the stuck-watchdog and turned
    partial evidence into an ERROR. It must time out, and not queue a second read."""
    class WedgedLogs(WedgedSession):
        def __init__(self):
            super().__init__()
            self.reads = []
        def get_log(self, kind):
            self.reads.append(kind)
            self._release.wait(timeout=30)
            return []

    sess = WedgedLogs()
    d = AppiumDriver(sess, tmp_path, remote=True)
    d._cmd_timeout = 0.2
    try:
        await asyncio.wait_for(d.capture_diagnostics(), timeout=5)
        assert sess.reads == ["syslog"]  # timed out once; did not queue the crashlog read
    finally:
        sess.release()


async def test_simulator_reconnect_opens_a_fresh_session_without_reinstalling(tmp_path):
    """A wedged simulator session used to end the run ('reconnect not supported'). The
    hook quits the old session (bounded) and opens a new one that never reinstalls."""
    from tamam.executors.ios.providers.simulator import SimulatorProvider

    class Opts:
        def __init__(self): self.caps = {}
        def set_capability(self, k, v): self.caps[k] = v

    class OldSession(WedgedSession):
        capabilities = {}
        def update_settings(self, d): pass

    class NewSession:
        session_id = "new"
        capabilities = {}
        def update_settings(self, d): pass

    class WD:
        calls = []
        @staticmethod
        def Remote(server, options):
            WD.calls.append(dict(options.caps))
            return NewSession()

    old = OldSession()
    prov = SimulatorProvider(server_url="http://127.0.0.1:4723")
    prov._driver = AppiumDriver(old, tmp_path)
    prov._driver._cmd_timeout = 0.2
    opts = Opts()
    opts.set_capability("enforceAppInstall", True)
    try:
        await asyncio.wait_for(prov._make_reconnect(WD, opts)(), timeout=5)
    finally:
        old.release()
    assert old.quit_started
    assert prov._driver._d.session_id == "new"
    caps = WD.calls[0]
    assert caps["noReset"] is True and caps["enforceAppInstall"] is False


async def test_settings_webdriveragent_silently_drops_are_reported(tmp_path):
    """WDA answers 200 to settings it doesn't know and drops them. On Device Farm's Appium 2
    that silently disabled snapshotMaxChildren (4.9 MB trees, wedged runs). Read back."""
    class OldWDA:
        capabilities = {}
        def __init__(self): self.s = {}
        def update_settings(self, d):
            self.s.update({k: v for k, v in d.items() if k != "snapshotMaxChildren"})
        def get_settings(self): return dict(self.s)

    d = AppiumDriver(session=OldWDA(), artifacts_dir=str(tmp_path))
    await d._tune_perception()
    assert d.dropped_settings == ["snapshotMaxChildren"]


async def test_no_dropped_settings_on_a_current_webdriveragent(tmp_path):
    class NewWDA:
        capabilities = {}
        def __init__(self): self.s = {}
        def update_settings(self, d): self.s.update(d)
        def get_settings(self): return dict(self.s)

    d = AppiumDriver(session=NewWDA(), artifacts_dir=str(tmp_path))
    await d._tune_perception()
    assert d.dropped_settings == []



async def test_a_local_simulator_waits_less_for_idle_than_a_farm_device(tmp_path):
    """the simulator settles by screenshot after each action, so XCUITest's idle wait
    is mostly double-counted there; a farm device keeps the full wait (typed text lagged)."""
    class WDA:
        capabilities = {}
        def __init__(self): self.s = {}
        def update_settings(self, d): self.s.update(d)
        def get_settings(self): return dict(self.s)

    sim, farm = WDA(), WDA()
    await AppiumDriver(session=sim, artifacts_dir=str(tmp_path))._tune_perception()
    await AppiumDriver(session=farm, artifacts_dir=str(tmp_path), remote=True)._tune_perception()
    assert sim.s["waitForIdleTimeout"] == 0.2
    assert farm.s["waitForIdleTimeout"] == 1.0

class _CapsRecorder:
    def __init__(self):
        self.caps = {}

    def set_capability(self, k, v):
        self.caps[k] = v


def test_host_ports_keep_appium_defaults_when_unset(monkeypatch):
    """One session per Mac (the common case): wdaLocalPort is not sent, MJPEG on 9100."""
    from tamam.config import get_settings
    from tamam.executors.ios.providers.common import apply_host_ports

    monkeypatch.delenv("TAMAM_IOS_WDA_LOCAL_PORT", raising=False)
    monkeypatch.delenv("TAMAM_IOS_MJPEG_SERVER_PORT", raising=False)
    get_settings.cache_clear()
    opts = _CapsRecorder()
    assert apply_host_ports(opts) == 9100
    assert "wdaLocalPort" not in opts.caps
    assert opts.caps["mjpegServerPort"] == 9100


def test_host_ports_are_configurable_for_a_second_session(monkeypatch):
    """A second XCUITest session on the same Mac needs its own WDA ports, or its
    commands reach the other session's WebDriverAgent."""
    from tamam.config import get_settings
    from tamam.executors.ios.providers.common import apply_host_ports

    monkeypatch.setenv("TAMAM_IOS_WDA_LOCAL_PORT", "8101")
    monkeypatch.setenv("TAMAM_IOS_MJPEG_SERVER_PORT", "9101")
    get_settings.cache_clear()
    opts = _CapsRecorder()
    assert apply_host_ports(opts) == 9101
    assert opts.caps["wdaLocalPort"] == 8101
    assert opts.caps["mjpegServerPort"] == 9101


def test_a_swipe_on_an_element_starts_inside_it_and_travels_past_it():
    """a card narrower than the screen: the old route started at 85% of the SCREEN
    (on the background), so the card never moved."""
    from tamam.executors.core.contracts import Element, Frame
    from tamam.executors.ios.driver import swipe_route
    card = Element(ref="e1", type="Other", label="Swipe me left", frame=Frame(100, 300, 200, 120))
    (fx, fy), (tx, ty) = swipe_route(card, "left", 402, 874)
    assert 100 <= fx <= 300 and fy == ty                 # starts ON the card
    assert tx < 100                                      # ends past its left edge
    row = Element(ref="e2", type="ScrollView", label="Categories", frame=Frame(0, 200, 402, 44))
    (fx, _), (tx, _) = swipe_route(row, "left", 402, 874)
    assert fx > 300 and tx < 120                         # a full-width row still sweeps across


async def test_typing_into_a_field_the_tree_hides_falls_back_to_the_keyboard():
    """a flattened form exposes no text-field element; type like a real keyboard."""
    from tamam.executors.ios.driver import AppiumDriver
    calls = []

    class D:
        class switch_to:
            active_element = None
        def find_elements(self, *a):
            return []
        def execute_script(self, name, args=None):
            calls.append((name, args))

    drv = AppiumDriver.__new__(AppiumDriver)
    drv._d = D()
    async def guard(fn, *a, what="", timeout=None):
        return fn(*a)
    drv._guard = guard
    await drv.type_focused("Ada")
    assert ("mobile: keys", {"keys": ["A", "d", "a"]}) in calls


async def test_clearing_a_hidden_field_holds_the_keyboards_delete_key():
    """a flattened RN login form: 'clear' was ignored on the keyboard fallback, so a password typed
    into the still-focused email field could not be removed. mobile: keys cannot backspace
    (probe: \\b, DEL and the named key all did nothing); holding the delete key does."""
    from tamam.executors.ios.driver import AppiumDriver

    class S:
        capabilities = {}
        def __init__(self): self.calls = []
        @property
        def page_source(self):
            return ('<XCUIElementTypeApplication><XCUIElementTypeKey type="XCUIElementTypeKey" name="delete" '
                    'label="delete" x="348" y="698" width="52" height="54"/></XCUIElementTypeApplication>')
        def execute_script(self, name, args=None):
            self.calls.append((name, args))
            if name == "mobile: keys" and not self.calls[:-1]:
                pass
        class switch_to:                        # no active element: the field is hidden
            active_element = None
        def find_elements(self, *a, **k): return []

    sess = S()
    d = AppiumDriver(session=sess, artifacts_dir="/tmp")
    await d.type_focused("Ada", clear=True)
    names = [c[0] for c in sess.calls]
    assert "mobile: touchAndHold" in names and names[-1] == "mobile: keys"
    hold = next(c[1] for c in sess.calls if c[0] == "mobile: touchAndHold")
    assert (hold["x"], hold["y"]) == (374.0, 725.0)
    sess.calls.clear()
    await d.type_focused("Ada")                        # no clear: the keys only
    assert [c[0] for c in sess.calls if c[0] != "mobile: keys"] == []
