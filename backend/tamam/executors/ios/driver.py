"""AppiumDriver — our hands on iOS, and the artifact owner.

Appium is pure actuation behind the DeviceDriver seam: tap/type/swipe by
coordinate (from the perceiver's frames), plus screenshots, screen recording, and
device/crash logs as evidence. It never makes a decision. Blocking Appium calls
run in a thread so the async loop (live preview, events) stays responsive.

The same driver talks to a local simulator or a farm — only the server URL and
capabilities differ (that lives in the provider).
"""

from __future__ import annotations

import asyncio
import time
import logging
import signal
from pathlib import Path
from typing import Any

from tamam.executors.core.contracts import Element, Frame

logger = logging.getLogger(__name__)


class DriverTimeout(Exception):
    """A single driver command exceeded its per-call budget. Raised so the executor
    can recover (clear a blocking modal, retry, or end with partial evidence) instead
    of the whole run stalling until the stuck-watchdog fires. The underlying worker
    thread is left to finish on its own — the loop just stops waiting on it."""


class DriverSessionDead(Exception):
    """The device's Appium/WDA session no longer exists mid-run (app crash, WDA recycle).
    Raised so the executor can reconnect (up to a cap) and continue, rather than erroring
    the whole run. Distinct from DriverTimeout (a hang) and ordinary command errors."""


# Where macOS writes crash reports for apps running in a SIMULATOR. The simulator is a
# host process, so its crashes never appear in the device's own crash log — which is why
# an app can die mid-run and the session look merely "not running".
HOST_CRASH_DIR = Path.home() / "Library" / "Logs" / "DiagnosticReports"


def _summarize_crash(path: Path) -> str | None:
    """One line a human can act on: what died, how, and the top frame that explains it."""
    import json as _json

    try:
        with path.open(errors="replace") as fh:
            meta = _json.loads(fh.readline())
            body = _json.loads(fh.read())
    except Exception:
        return None
    exc = body.get("exception", {}) or {}
    what = "/".join(x for x in (exc.get("type"), exc.get("signal")) if x) or "crash"
    # An uncaught exception's own backtrace names the real culprit; the thread backtrace
    # only ever shows abort().
    frames = body.get("lastExceptionBacktrace") or []
    culprit = next((f.get("symbol") for f in frames
                    if f.get("symbol") and not f["symbol"].startswith(("__exception", "objc_exception"))), "")
    name = meta.get("app_name", path.stem)
    ver = meta.get("app_version", "?")
    return f"{name} {ver} crashed ({what}){' in ' + culprit if culprit else ''} [{path.name}]"


def host_crash_reports(bundle_id: str, since_epoch: float, limit: int = 5) -> list[str]:
    """Crash reports this app produced on the host since the session started."""
    if not bundle_id:
        return []
    import json as _json

    out: list[str] = []
    try:
        files = sorted(HOST_CRASH_DIR.glob("*.ips"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:60]
    except Exception as exc:
        logger.debug("cannot read host crash reports: %s", exc)
        return []
    for f in files:
        try:
            if f.stat().st_mtime < since_epoch:
                continue
            with f.open(errors="replace") as fh:
                if _json.loads(fh.readline()).get("bundleID") != bundle_id:
                    continue
        except Exception:
            continue
        line = _summarize_crash(f)
        if line:
            out.append(line)
        if len(out) >= limit:
            break
    return out


def _looks_like_dead_session(exc: Exception) -> bool:
    s = str(exc).lower()
    return ("invalid session id" in s or "session does not exist" in s
            or "session is either terminated or not started" in s
            # the app itself died (crash, or a resume that never came back): recoverable
            # by relaunching, so it must not surface as a raw executor error
            or "is not running, possibly crashed" in s
            or type(exc).__name__ == "InvalidSessionIdException")


def swipe_route(element: Element, direction: str, screen_w: int, screen_h: int
                ) -> tuple[tuple[int, int], tuple[int, int]]:
    """Start INSIDE the element (a swipe that starts on the background moves nothing: a card
    narrower than the screen never moved,) and travel well past it — far enough to scroll
    a row or flick a card aside."""
    f = element.frame
    x, y = element.tap_point
    lo_x, hi_x = int(f.x + 0.15 * f.width), int(f.x + 0.85 * f.width)
    lo_y, hi_y = int(f.y + 0.15 * f.height), int(f.y + 0.85 * f.height)
    reach_x, reach_y = max(int(0.6 * screen_w), int(f.width)), max(int(0.5 * screen_h), int(f.height))
    clamp_x = lambda v: max(5, min(screen_w - 5, v))  # noqa: E731
    clamp_y = lambda v: max(5, min(screen_h - 5, v))  # noqa: E731
    return {
        "left":  ((hi_x, y), (clamp_x(hi_x - reach_x), y)),   # content moves left
        "right": ((lo_x, y), (clamp_x(lo_x + reach_x), y)),
        "up":    ((x, hi_y), (x, clamp_y(hi_y - reach_y))),
        "down":  ((x, lo_y), (x, clamp_y(lo_y + reach_y))),
    }[direction]


class AppiumDriver:
    def __init__(self, session: Any, artifacts_dir: str | Path, *,
                 remote: bool = False, local_simulator: bool = True) -> None:
        self._d = session  # appium webdriver.Remote
        self._dir = Path(artifacts_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        # remote=True → a farm device (Sauce): it records + streams on its side, so we
        # skip the local MJPEG governor. local_simulator=True → screen recording via
        # `simctl` (simulators only; a real device or farm has no local simctl video).
        self.remote = remote
        self._local_simulator = local_simulator
        self._w = 0
        self._h = 0
        self._started_at = 0.0
        self._shots = 0
        self._udid: str | None = None
        self._rec_proc: Any = None
        self._video_path: str | None = None
        self.mjpeg_url: str | None = None  # set by the provider; live preview reads it
        self.session_id: str | None = None
        # Remote video fetcher: provider sets an async fn(session_id) -> url|None that
        # pulls the farm's recording after the session ends.
        self.remote_video_fetcher: Any = None
        # Set by the provider (clean-state cloud runs): uninstall the app at teardown so
        # no authenticated session lingers on a shared device (BrowserStack neutralizes
        # Appium fullReset, so uninstall-on-teardown is the reliable keychain clear).
        self.teardown_uninstall: bool = False
        self.teardown_uninstall_bundle: str | None = None  # hint; resolved from caps if unset
        self._app_bundle: str | None = None
        self._device_errors: list[str] = []
        self._crashes: list[str] = []
        # One Appium session processes one command at a time; the live-preview governor
        # and the per-step capture both screenshot the SAME session. Without this lock
        # the concurrent calls collide and per-step capture fails silently (no image →
        # vision goes blind). Serialize all screenshotting through it.
        self._shot_lock = asyncio.Lock()
        from tamam.config import get_settings
        _s = get_settings()
        self._cmd_timeout = _s.driver_command_timeout_s
        self._read_timeout = _s.driver_read_timeout_s
        # Provider-supplied coroutine that re-establishes the underlying session (self._d)
        # on the SAME endpoint after a crash. None → reconnect unsupported for this backend.
        self._reconnect: Any = None
        self.call_log: list[dict] = []
        self.dropped_settings: list[str] = []  # settings WDA accepted but silently ignored  # every _guard'ed device command, timed (drained per step)

    async def reconnect(self) -> None:
        """Re-establish a dead Appium/WDA session (app crash) without re-provisioning.
        Delegates to the provider hook; raises if this backend can't reconnect."""
        if self._reconnect is None:
            raise DriverSessionDead("reconnect not supported for this backend")
        await self._reconnect()

    async def _guard(self, fn: Any, *args: Any, what: str, timeout: float | None = None) -> Any:
        """Run a blocking Appium call in a thread under a hard timeout. A hung call
        (a modal blocking WDA, a wedged session) raises DriverTimeout instead of
        stalling the loop forever. The orphaned thread is abandoned — the session is
        one-command-at-a-time, so the executor should recover or end after a timeout.
        `timeout` overrides the default per-command budget (the tree read gets more)."""
        budget = timeout or self._cmd_timeout
        t0 = time.monotonic()
        try:
            out = await asyncio.wait_for(asyncio.to_thread(fn, *args), budget)
            self._record(what, t0, "ok", out)
            return out
        except (asyncio.TimeoutError, TimeoutError) as exc:
            self._record(what, t0, "timeout")
            raise DriverTimeout(f"{what} timed out after {budget}s") from exc
        except Exception as exc:
            self._record(what, t0, "error")
            # A dead device session (app/WDA crash) is recoverable by reconnecting —
            # surface it distinctly so the executor can, not swallow it as a generic error.
            if _looks_like_dead_session(exc):
                raise DriverSessionDead(f"{what}: {str(exc)[:160]}") from exc
            raise

    def _record(self, what: str, t0: float, status: str, out: Any = None) -> None:
        """Every device command, timed — the trace that says where a step's seconds went."""
        entry = {"cmd": what, "s": round(time.monotonic() - t0, 2), "status": status}
        if what == "accessibility" and isinstance(out, str):
            entry["kb"] = round(len(out) / 1024, 1)
        self.call_log.append(entry)

    async def start(self) -> None:
        # Route the FIRST command through _guard: a cloud device can hand back a session
        # that has already gone stale (slow provisioning / long device wait) before we
        # issue anything, and get_window_size is the first thing we call. Guarding it means
        # a dead session surfaces as DriverSessionDead (recoverable via the provider's
        # reconnect hook) instead of a raw InvalidSessionIdException that errors the whole
        # run — otherwise the entire provisioned session (and its device-minutes) is wasted.
        import time as _time
        self._started_at = _time.time()
        size = await self._guard(self._d.get_window_size, what="get-window-size")
        self._w, self._h = int(size["width"]), int(size["height"])
        caps = getattr(self._d, "capabilities", None) or {}
        # Appium normalizes/prefixes returned cap keys, so the udid can come back under
        # several names — check them all, then fall back to the configured (pinned) udid.
        # Getting this right is what enables simulator screen recording (recordVideo needs
        # a udid); a single-key `caps.get("udid")` silently returned None → no video.
        from tamam.config import get_settings
        self._udid = next(
            (caps[k] for k in ("udid", "appium:udid", "deviceUDID", "appium:deviceUDID",
                               "appium:deviceUdid") if caps.get(k)),
            None,
        ) or (get_settings().ios_device_udid or None)
        # The app-under-test bundle id — for teardown uninstall when the test didn't
        # set one explicitly (a build-based test relies on the .ipa's own bundle id,
        # which the farm fills into the session capabilities).
        self._app_bundle = next(
            (caps[k] for k in ("bundleId", "bundleID", "appium:bundleId", "appium:bundleID",
                               "CFBundleIdentifier") if caps.get(k)),
            None,
        )
        self.session_id = getattr(self._d, "session_id", None)
        await self._tune_perception()
        await self._start_recording()

    def _idle_wait(self, s: Any) -> float:
        """Local simulators settle by screenshot after each action, so they need less of
        XCUITest's own idle wait; farm devices keep the full wait (typed text lagged without it)."""
        if self.remote or not self._local_simulator:
            return s.ios_wait_for_idle_s
        return min(s.ios_wait_for_idle_s, s.ios_wait_for_idle_local_s)

    async def _tune_perception(self) -> None:
        """Make page_source fast on heavy screens (general, app-agnostic). By default WDA
        walks the WHOLE tree and waits for the app to be idle before returning source, so a
        modal-over-a-feed or a keyboard-up screen can blow past our command timeout on a
        farm. Cap the depth and stop waiting for quiescence. Best-effort: a provider that
        rejects a setting just keeps the default."""
        from tamam.config import get_settings
        s = get_settings()
        wanted = {
            "snapshotMaxDepth": s.ios_snapshot_max_depth,
            "snapshotMaxChildren": s.ios_snapshot_max_children,
            "waitForIdleTimeout": self._idle_wait(s),
            "customSnapshotTimeout": max(10, self._cmd_timeout - 5),
        }
        if not s.ios_auto_accept_alerts:
            # With autoAcceptAlerts off, an iOS permission prompt would sit over the app
            # unseen (page source reads the app, not SpringBoard). This makes the read
            # return the system dialog instead, so the perceiver can accept and report it.
            wanted["respectSystemAlerts"] = True
        try:
            await self._guard(self._d.update_settings, wanted, what="update-settings")
        except Exception:
            logger.warning("could not apply perception perf settings; using defaults", exc_info=True)
            return
        # Read them back. WebDriverAgent answers 200 to settings it does not know and simply
        # drops them — on Device Farm's Appium 2 that silently disabled snapshotMaxChildren
        # and cost a day of wedged runs. A dropped setting is now loud and on the record.
        try:
            got = await self._guard(self._d.get_settings, what="get-settings")
        except Exception:
            return
        self.dropped_settings = sorted(k for k in wanted if k != "customSnapshotTimeout"
                                       and isinstance(got, dict) and k not in got)
        if self.dropped_settings:
            logger.warning("WebDriverAgent ignored settings %s — perception will not be "
                           "bounded as configured (old WDA / Appium version?)",
                           self.dropped_settings)

    async def _start_recording(self) -> None:
        """Record the simulator screen via simctl — ffmpeg-free, H.264 mp4, and
        independent of the Appium command session (no contention)."""
        if self.remote or not self._local_simulator:
            return  # farm records on its side; real devices have no local simctl video
        if not self._udid:
            logger.warning("no udid on session; screen recording skipped")
            return
        self._video_path = str(self._dir / "recording.mp4")
        try:
            self._rec_proc = await asyncio.create_subprocess_exec(
                "xcrun", "simctl", "io", self._udid, "recordVideo",
                "--codec", "h264", "--force", self._video_path,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
        except Exception as exc:
            logger.warning("simctl recording unavailable: %s", exc)
            self._rec_proc = None

    async def tap(self, element: Element) -> None:
        x, y = element.tap_point  # occlusion-adjusted when a control covers the centre
        await self._guard(self._d.execute_script, "mobile: tap", {"x": x, "y": y}, what="tap")

    async def long_press(self, element: Element, seconds: float = 1.0) -> None:
        """Touch and hold an element (a context menu, a "hold to confirm" button)."""
        x, y = element.tap_point
        await self._guard(self._d.execute_script, "mobile: touchAndHold",
                          {"x": x, "y": y, "duration": float(seconds)}, what="long-press")

    async def double_tap(self, element: Element) -> None:
        """Two real touches 100 ms apart in ONE gesture. XCUITest's doubleTap was not counted as
        two taps by React Native buttons (12 attempts on an RN gestures screen,)."""
        x, y = element.tap_point
        await self._guard(self._two_taps, x, y, what="double-tap")

    def _two_taps(self, x: int, y: int) -> None:
        from selenium.webdriver.common.actions import interaction
        from selenium.webdriver.common.actions.action_builder import ActionBuilder
        from selenium.webdriver.common.actions.pointer_input import PointerInput
        finger = PointerInput(interaction.POINTER_TOUCH, "finger")
        ab = ActionBuilder(self._d, mouse=finger)
        # 0.1 s press, 0.1 s gap: measured on a React Native double-tap (two onPress within
        # 300 ms) — 0.1/0.1 and 0/0.08 pass; 0.05 s presses and XCUITest doubleTap do not.
        (ab.pointer_action.move_to_location(x, y).pointer_down().pause(0.1).pointer_up()
         .pause(0.1).pointer_down().pause(0.1).pointer_up())
        ab.perform()

    async def tap_point(self, x: int, y: int) -> None:
        """Tap a raw screen COORDINATE — the last-resort path for targets with no element
        (rich-editor text the a11y tree doesn't expose)."""
        await self._guard(self._d.execute_script, "mobile: tap", {"x": int(x), "y": int(y)},
                          what="tap_point")

    async def type(self, element: Element, text: str, clear: bool = False) -> None:
        x, y = element.tap_point
        await self._guard(self._d.execute_script, "mobile: tap", {"x": x, "y": y}, what="tap")
        # Pass the tap point so the fallback can target the field the brain actually chose.
        # `clear` replaces a prefilled value instead of appending to it.
        await self._guard(self._send_text, text, x, y, clear, what="type")

    async def back(self) -> None:
        """The system back gesture: a pan from the left screen edge. Works on any pushed
        screen whether or not its back button is exposed (custom nav bars often hide it)."""
        y = int(self._h * 0.5)
        await self._guard(
            self._d.execute_script, "mobile: dragFromToForDuration",
            {"fromX": 1, "fromY": y, "toX": int(self._w * 0.75), "toY": y, "duration": 0.3},
            what="back")

    async def clear_focused(self, seconds: float = 3.0) -> bool:
        """Delete the focused field's text by HOLDING the keyboard's delete key (iOS speeds up
        to whole words). For a field the tree hides: there is no element to clear, and
        `mobile: keys` cannot send a backspace (probe: '\\b', DEL and the named key all did
        nothing; a 3 s hold cleared 33 characters). False when no delete key is on screen."""
        import re
        src = await self.accessibility()
        m = re.search(r'<XCUIElementTypeKey\b[^>]*name="delete"[^>]*x="([\d.]+)" y="([\d.]+)" '
                      r'width="([\d.]+)" height="([\d.]+)"', src)
        if m is None:
            return False
        x, y, w, h = map(float, m.groups())
        key = Element(ref="", type="Key", label="delete", frame=Frame(x, y, w, h))
        await self.long_press(key, seconds)
        return True

    async def type_focused(self, text: str, clear: bool = False) -> None:
        """Type into the CURRENTLY-FOCUSED field without tapping first — for a rich-editor
        field (no element ref) already focused by a prior tap_point. Clears existing content
        first: this path is used to EDIT/replace a field, and appending would corrupt it
        (e.g. "Edit target alpha" + "Edit target beta" → a merged title)."""
        try:
            await self._guard(self._send_text, text, None, None, True, what="type")
        except DriverSessionDead:
            raise
        except Exception:
            # No text-field element to type into: the form is hidden from the accessibility tree
            # (a merged / flattened view — one RN registration form showed 2 elements).
            # Type like a real keyboard into whatever has focus (the field the agent tapped).
            # Measured: the text lands, and OCR of the next screen reads it back.
            if clear:
                # The fallback types like a keyboard, which appends: "replace" must delete the
                # old text first (a password typed into a still-focused email field could
                # not be cleared, and 12 "clear" retypes only made the email longer).
                await self.clear_focused()
            await self._guard(self._d.execute_script, "mobile: keys", {"keys": list(text)},
                              what="type-keys")

    def _send_text(self, text: str, tap_x: int | None = None, tap_y: int | None = None,
                   clear_first: bool = False) -> None:
        """Type into the field the brain targeted (tapped at tap_x/tap_y). `active_element`
        works for standard fields but returns null for some SwiftUI editors (e.g. a multi-line
        TextEditor), so we fall back to locating a visible text input directly — but we must
        target the RIGHT one: on a multi-field form, blindly typing into the first accepting
        input silently fills the wrong box. So prefer the input whose rect contains the tap
        point, and only then fall back to the rest."""
        try:
            active = self._d.switch_to.active_element
            if active:
                if clear_first:
                    try:
                        active.clear()
                    except Exception:
                        pass
                active.send_keys(text)
                return
        except Exception:
            pass
        from appium.webdriver.common.appiumby import AppiumBy

        # Collect candidate text inputs. We deliberately do NOT gate on is_displayed():
        # XCUITest reports SwiftUI TextEditors as not-displayed even when they're the
        # visible, focused editor — the only reliable signal is whether send_keys succeeds.
        candidates = []
        for cls in ("XCUIElementTypeTextView", "XCUIElementTypeTextField",
                    "XCUIElementTypeSecureTextField"):
            candidates.extend(self._d.find_elements(AppiumBy.CLASS_NAME, cls))

        def _contains_tap(el) -> bool:
            if tap_x is None or tap_y is None:
                return False
            try:
                r = el.rect
                return (r["x"] <= tap_x <= r["x"] + r["width"]
                        and r["y"] <= tap_y <= r["y"] + r["height"])
            except Exception:
                return False

        # The field under the tap point first (the intended target), then the others as a
        # last resort — so we never silently type into an unrelated field.
        for el in sorted(candidates, key=lambda e: 0 if _contains_tap(e) else 1):
            try:
                if clear_first:
                    try:
                        el.clear()
                    except Exception:
                        pass
                el.send_keys(text)
                return
            except Exception:
                continue
        raise RuntimeError("no text field found to type into")

    async def swipe(self, direction: str, element: Element | None = None) -> None:
        """Swipe the whole window, or — when an element is given — swipe ACROSS THAT
        ELEMENT's band, which is what scrolls the row/carousel it belongs to.

        A window-level `mobile: swipe` is applied to the app as a whole, so on a screen
        whose body is a list, a horizontal flick lands on the list and can navigate
        instead of scrolling the strip the agent meant. Dragging along the element's own
        centre line keeps the gesture inside that row."""
        if element is None:
            await self._guard(
                self._d.execute_script, "mobile: swipe", {"direction": direction}, what="swipe")
            return
        route = swipe_route(element, direction, self._w, self._h)
        (fx, fy), (tx, ty) = route
        # A FLICK, the way a finger swipes: a brief touch, then a fast move. The old 0.4 s
        # press-then-drag read as a long-press to many swipe handlers (an RN swipe card
        # never advanced,).
        await self._guard(
            self._d.execute_script, "mobile: dragFromToWithVelocity",
            {"fromX": fx, "fromY": fy, "toX": tx, "toY": ty, "pressDuration": 0.05,
             "holdDuration": 0.05, "velocity": 2200}, what="swipe-element")

    async def drag(self, frm: Element, to: Element | None = None, to_xy: tuple[int, int] | None = None,
                   ) -> None:
        """Press, hold, and move slowly to a target: drag-and-drop (reorder, slider thumb)."""
        fx, fy = frm.tap_point
        tx, ty = to.tap_point if to is not None else to_xy
        await self._guard(
            self._d.execute_script, "mobile: dragFromToWithVelocity",
            {"fromX": fx, "fromY": fy, "toX": int(tx), "toY": int(ty), "pressDuration": 0.6,
             "holdDuration": 0.2, "velocity": 400}, what="drag")

    async def background_app(self, seconds: float = 2.0) -> None:
        """Send the app to the background and bring it back — the lifecycle event that
        exposes state that was never persisted (lost filters, dropped sessions, blank
        screens on resume). XCUITest restores the app after `seconds`."""
        await self._guard(self._d.execute_script, "mobile: backgroundApp",
                          {"seconds": seconds}, what="background-app")
        # XCUITest is supposed to restore the app after `seconds`, but whether it comes
        # back to the foreground is not guaranteed. Ask for it explicitly — activating an
        # already-active app is a no-op, so this only ever removes ambiguity.
        bundle = self.teardown_uninstall_bundle or self._app_bundle
        if bundle:
            await self._guard(self._d.execute_script, "mobile: activateApp",
                              {"bundleId": bundle}, what="activate-app")

    async def hide_keyboard(self) -> bool:
        """Dismiss the on-screen keyboard so it stops bloating the a11y tree (a huge
        page_source cost after typing). Only DISMISS keys are pressed: return/Go/Search/
        Next/Send are ACTIONS (they submit a form, send a message, run a search, insert a
        newline) — the harness pressing them is a side effect the agent never chose.
        Guarded and best-effort — returns False if there's nothing to dismiss."""
        try:
            await self._guard(
                self._d.execute_script, "mobile: hideKeyboard",
                {"keys": ["Done", "done", "Dismiss"]},
                what="hide-keyboard",
            )
            return True
        except Exception:
            return False

    async def accessibility(self) -> str:
        # the heavy read — give it the larger read budget, not the tight command timeout
        return await self._guard(lambda: self._d.page_source, what="accessibility",
                                 timeout=self._read_timeout)

    async def screenshot(self) -> str:
        self._shots += 1
        path = self._dir / f"shot_{self._shots}.png"
        async with self._shot_lock:
            png = await self._guard(self._d.get_screenshot_as_png, what="screenshot")
        path.write_bytes(png)
        return str(path)

    async def screenshot_bytes(self) -> bytes:
        async with self._shot_lock:
            return await self._guard(self._d.get_screenshot_as_png, what="screenshot")

    def screen_size(self) -> tuple[int, int]:
        return self._w, self._h

    async def dismiss_alert(self) -> str | None:
        # A blocking system dialog (e.g. iOS notification-permission after saving with
        # Push on) is the classic cause of a wedged WDA; accepting it unblocks the loop.
        # Returns the alert's text (so the run can REPORT what was accepted on the
        # agent's behalf), or None when there was no alert. Guarded so this recovery
        # step can't itself hang.
        try:
            alert = await self._guard(lambda: self._d.switch_to.alert, what="alert-read")
        except Exception:
            return None
        try:
            text = await self._guard(lambda: alert.text, what="alert-text")
        except Exception:
            text = ""
        try:
            await self._guard(alert.accept, what="alert-accept")
        except Exception:
            return None
        return (text or "").strip() or "(untitled alert)"

    async def capture_diagnostics(self) -> None:
        """Pull syslog (error lines) + crash logs while the session is alive.

        BOUNDED: an unguarded get_log on a wedged session blocked past the stuck-watchdog
        and turned a run's partial evidence into an ERROR. A timed-
        out read means the session is wedged — the next read would only queue behind it,
        so skip straight to the host crash reports, which need no session at all."""
        wedged = False
        try:
            syslog = await self._guard(self._d.get_log, "syslog", what="syslog")
            self._device_errors = [
                e.get("message", "") for e in syslog
                if "error" in str(e.get("level", "")).lower() or "error" in str(e.get("message", "")).lower()
            ][:50]
        except DriverTimeout as exc:
            wedged = True
            logger.warning("syslog capture timed out (%s); skipping session log reads", exc)
        except Exception as exc:
            logger.debug("syslog capture failed: %s", exc)
        if not wedged:
            try:
                crash = await self._guard(self._d.get_log, "crashlog", what="crashlog")
                self._crashes = [str(e.get("message", e)) for e in crash][:20]
            except Exception as exc:
                logger.debug("device crashlog unavailable (%s); falling back to host reports", exc)
        if self._local_simulator:
            host = await asyncio.to_thread(
                host_crash_reports, self._app_bundle or "", self._started_at)
            if host:
                logger.warning("app crashed during this run: %s", host[0])
                self._crashes.extend(h for h in host if h not in self._crashes)

    def logs(self) -> tuple[list[str], list[str]]:
        return list(self._device_errors), list(self._crashes)

    async def stop(self) -> str | None:
        # CRITICAL: teardown must be BOUNDED. `quit`/`remove_app` on a WEDGED session — the
        # exact condition that trips the watchdog/DriverTimeout — would otherwise block
        # forever in a thread, so the executor's `finally` never returns and the run hangs
        # in RUNNING despite a guardrail deciding to end it. Every call here is wrapped in a
        # timeout and is best-effort (teardown failures never propagate; they only cost a
        # leaked farm session, which the provider/farm reclaims).
        video_path = await self._stop_recording()
        sid = self.session_id
        # Hygiene: remove the app (and its keychain/session) BEFORE ending the session,
        # so nothing authenticated lingers on a shared cloud device. Uninstall clears
        # the app's keychain on iOS 10.3+ — the reliable clear, since BrowserStack
        # neutralizes Appium fullReset. Bundle from the provider hint or session caps.
        if self.teardown_uninstall:
            bundle = self.teardown_uninstall_bundle or self._app_bundle
            if bundle:
                await self._teardown_step(
                    lambda: self._d.remove_app(bundle), self._cmd_timeout,
                    f"teardown: uninstalled {bundle} to clear session", "teardown app uninstall")
            else:
                logger.warning("teardown uninstall requested but no bundle id resolved")
        # end the session so the farm finalizes its video
        await self._teardown_step(self._d.quit, self._cmd_timeout, None, "session quit")
        if video_path is None and self.remote_video_fetcher and sid:
            try:
                video_path = await asyncio.wait_for(
                    self.remote_video_fetcher(sid), timeout=self._read_timeout)
            except (Exception, asyncio.TimeoutError) as exc:
                logger.warning("remote video fetch failed/timed out: %s", type(exc).__name__)
        return video_path

    async def _teardown_step(self, fn, timeout: float, ok_msg: str | None, what: str) -> None:
        """Run one best-effort, time-bounded teardown call. Never raises: a wedged session
        must not turn cleanup into a permanent hang."""
        try:
            await asyncio.wait_for(asyncio.to_thread(fn), timeout=timeout)
            if ok_msg:
                logger.info(ok_msg)
        except (Exception, asyncio.TimeoutError) as exc:
            logger.warning("%s failed/timed out: %s", what, type(exc).__name__)

    async def _stop_recording(self) -> str | None:
        if self._rec_proc is None:
            return None
        try:
            self._rec_proc.send_signal(signal.SIGINT)  # SIGINT finalizes the mp4
            await asyncio.wait_for(self._rec_proc.wait(), timeout=10)
        except Exception:
            try:
                self._rec_proc.kill()
            except Exception:
                pass
        p = self._video_path
        if p and Path(p).exists() and Path(p).stat().st_size > 0:
            return p
        logger.warning("simctl recording produced no file")
        return None
