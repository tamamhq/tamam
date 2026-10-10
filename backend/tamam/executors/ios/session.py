"""DeviceSession — one live device, read and driven with the harness's recovery rules.

Shared by the AI agent loop (executor.py) and test replay (tamam/graduation/replay.py), so the
hard-won handling of wedged and dead sessions exists once:
  • observe: survive a wedged read (DriverTimeout) or a dead session by reconnecting once;
    give a mid-transition blank screen a moment (bounded);
  • act: failures are OBSERVATIONS (a note), never exceptions that end the run — except a
    session that cannot be brought back, which raises SessionLost;
  • final: wait (bounded) for the screen to settle, then capture its text + screenshot together.
The reconnect budget belongs to the caller (it spans a whole run), passed in as a callable.
"""

from __future__ import annotations

import asyncio
import re
import dataclasses
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from tamam.executors.ios.brain import (Back, BackgroundApp, DoubleTap, Drag, HideKeyboard, LongPress,
                                     ScrollTo, Swipe, Tap, TapPoint, Type, Wait)

from tamam.executors.ios.grounder import input_box_below, ocr_lines
from tamam.executors.ios.perceiver import KEYBOARD, TEXT_INPUT_TYPES
from tamam.executors.ios.driver import DriverSessionDead, DriverTimeout
from tamam.executors.core.contracts import Element, Frame, SessionLost, screen_moved  # noqa: F401  (SessionLost re-exported)
from tamam.executors.ios.perceiver import parse_source, serialize

logger = logging.getLogger(__name__)

BENIGN_SHEETS = ("Save Password?", "Update Password?", "Save password?")
SCROLL_TO_MAX = 15      # swipes per scroll_to: one action, bounded well inside the stuck-watchdog
SCROLL_SETTLE_S = 0.4   # let a virtualized list re-render before reading it
_OPPOSITE = {"up": "down", "down": "up", "left": "right", "right": "left"}
KEYBOARD_CLOSE_S = 0.4  # the keyboard's closing animation, before re-reading the screen
KEYBOARD_RECHECK_S = 0.6  # a second read, to be sure it stayed gone
KEYBOARD_ZONE_FRAC = 0.45  # below this share of the screen, a keyboard could cover a tap
NAV_BAR_FRAC = 0.12     # the top of the screen that holds the status and navigation bars
_SECRET_LABEL = re.compile(r"pass(?:word|code|phrase)|\bpin\b|\bcvc\b|\bcvv\b", re.IGNORECASE)


def _state(e: Any) -> tuple:
    return (e.type, e.label, e.value, e.selected, e.toggled, e.filled, e.enabled)


MAX_WAIT_S = 10.0     # one `wait` never outlasts the stuck-watchdog


def _names(want: str, low: str) -> bool:
    """A text that IS the item (or starts with it), not a sentence that mentions it: the
    screen's own instruction ("Scroll to Item 70 and tap it") contains the text too and made
    scroll_to stop at once."""
    if low == want or low.startswith(want):
        return True
    return (want in low and len(low) <= len(want) + 24
            and not low.startswith(("scroll", "tap", "find", "swipe")))


def _scrolled(before: set, after: set) -> bool:
    """Did the screen's text move? A scroll replaces most lines; OCR noise on a still screen
    (a bounce frame, one misread line) changes one or two."""
    if not before and not after:
        return False
    return len(before ^ after) > max(len(before), len(after))


async def _noop_heartbeat() -> None:
    return None


def back_control(space: Any) -> Any:
    """An on-screen back control (nav-bar back button), when the screen has one."""
    for e in getattr(space, "elements", []):
        low = (e.label or "").strip().lower()
        if e.type == "Button" and (low in {"back", "chevron.left", "arrow.left",
                                           "chevron.backward", "arrow.backward"}):
            return e
    return None


def _contains(f: Frame, x: float, y: float) -> bool:
    return f.x <= x <= f.x + f.width and f.y <= y <= f.y + f.height


def blank_spot(space: Any) -> Element | None:
    """Somewhere to tap that takes focus off a text field and does nothing else, or None:
    plain text below the navigation bar (not the bar's title: tapping that leaves the field
    focused), else the top-left of a non-interactive container (a form's heading area), else
    the blank margin just under the nav bar if no element claims it."""
    below_nav = space.screen_h * NAV_BAR_FRAC
    text = min((e for e in space.elements if e.type == "StaticText" and not e.covered_by
                and e.frame.y >= below_nav), key=lambda e: e.frame.y, default=None)
    if text is not None:
        return text
    controls = [e.frame for e in space.elements if e.type not in ("Other", "StaticText", "Image")]
    for e in sorted((e for e in space.elements if e.type == "Other" and e.frame.y >= below_nav
                     and e.frame.width > 100 and e.frame.height > 60), key=lambda e: e.frame.y):
        x, y = e.frame.x + 12, e.frame.y + 12
        if not any(_contains(f, x, y) for f in controls):
            return dataclasses.replace(e, tap_x=x, tap_y=y)
    # Nothing at all to tap (with the keyboard up, a merged form leaves the tree): the blank
    # left margin under the nav bar (probe: it closed a flattened form's keyboard).
    x, y = 16.0, below_nav + 24
    if any(_contains(e.frame, x, y) for e in space.elements):
        return None
    return Element(ref="", type="Other", label="the blank margin under the nav bar",
                   frame=Frame(x - 1, y - 1, 2, 2))


def _credential_keyboard(space: Any) -> bool:
    return any((e.label or "").strip().lower() == "passwords" for e in space.elements)


class DeviceSession:
    def __init__(self, driver: Any, perceiver: Any,
                 reconnect: Callable[[], Awaitable[bool]]) -> None:
        self.driver = driver
        self.perceiver = perceiver
        self._reconnect = reconnect
        # The last action TIMED OUT: the one-command-at-a-time session may still be busy.
        self.act_timed_out = False

    # ── reading ──────────────────────────────────────────────────────────────

    async def observe(self, heartbeat=_noop_heartbeat) -> Any:
        """Observe, giving a mid-flight transition a moment. A screen with NO elements is
        the app pushing/popping or a tree not built yet — not a state worth reasoning
        about. Bounded, so a genuinely empty screen still proceeds."""
        space = await self._observe_resilient(heartbeat)
        tries = 0
        while space is not None and not space.elements and tries < 3:
            await asyncio.sleep(0.5 * (2 ** tries))
            await heartbeat()
            space = await self._observe_resilient(heartbeat)
            tries += 1
        if tries:
            logger.info("screen was blank; re-observed %d time(s), %d elements now",
                        tries, len(space.elements) if space else 0)
        return space

    async def _observe_resilient(self, heartbeat=_noop_heartbeat) -> Any:
        """Observe the screen, surviving a wedged driver call (DriverTimeout — a modal
        blocking WDA) or a dead session (DriverSessionDead — app/WDA crash). Recovers
        once; returns None only if still unusable, so the caller ends gracefully with
        the evidence gathered so far."""
        try:
            return await self.perceiver.observe(self.driver)
        except DriverTimeout as exc:
            # The abandoned call's THREAD is gone from our side, but Appium is
            # one-command-at-a-time per session, so the session is still busy building
            # that snapshot. Any command issued now simply queues behind it — which is
            # how a slow screen became a 120s silence and a dead run. Take a fresh
            # session instead of talking to a blocked one.
            logger.warning("observe timed out (%s); session is wedged, reconnecting", exc)
            await heartbeat()  # the first read consumed its (large) budget — keep the watchdog alive
        except DriverSessionDead as exc:
            logger.warning("observe hit a dead session (%s); reconnecting", exc)
            await heartbeat()
        if not await self._reconnect():
            return None
        try:
            return await self.perceiver.observe(self.driver)
        except Exception:
            return None

    async def screenshot(self) -> str | None:
        try:
            return await self.driver.screenshot()
        except Exception:
            return None

    async def _dismiss_benign_sheet(self, src: str) -> bool:
        """Decline a known iOS password-autofill sheet ('Save Password?', 'Update Password?')
        with its 'Not Now' button. Only these: anything else on screen is the app's, or a
        permission prompt the perceiver already handles."""
        if not src or not any(t in src for t in BENIGN_SHEETS):
            return False
        try:
            w, h = self.driver.screen_size()
            button = next((e for e in parse_source(src, w, h)
                           if e.type == "Button" and e.label in ("Not Now", "Not now")), None)
            if button is None:
                return False
            await self.driver.tap(button)
            logger.info("dismissed an iOS password sheet before the final capture")
            return True
        except Exception:
            return False

    async def final(self, heartbeat=_noop_heartbeat) -> tuple[str, str | None]:
        """Wait (bounded) for the screen to stop changing, then capture the final RENDERED
        text + screenshot TOGETHER — a stable end state, not a mid-transition frame. The
        text is the perceiver's VISIBLE element rendering (labels/values), NOT raw XCUITest
        XML: raw source made text checks match element-type names and off-screen nodes.
        Falls back to truncated raw source only if perception fails."""
        src = ""
        prev: str | None = None
        dismissed = False
        for _ in range(4):  # up to ~2s for the last transition to finish
            try:
                src = await self.driver.accessibility()
            except Exception:
                break
            await heartbeat()  # a slow settle read is work, not a stall
            if not dismissed and await self._dismiss_benign_sheet(src):
                # iOS's own "Save Password?" sheet covers the app after a login: the evidence
                # frame must be the APP's end state. "Not Now" stores nothing.
                dismissed, prev = True, None
                await asyncio.sleep(0.5)
                continue
            if src == prev:
                break  # screen settled
            prev = src
            await asyncio.sleep(0.5)
        shot = await self.screenshot()
        await heartbeat()
        rendered = ""
        # Reuse the source we just settled on rather than paying for another snapshot.
        if src:
            try:
                w, h = self.driver.screen_size()
                rendered = serialize(parse_source(src, w, h))
            except Exception:
                rendered = ""
        if not rendered:
            try:
                space = await self.perceiver.observe(self.driver)
                rendered = space.text or ""
            except Exception:
                logger.warning("final rendered-text capture failed; falling back to raw source")
                rendered = src[:6000]
        await heartbeat()
        return rendered[:6000], shot

    # ── acting ───────────────────────────────────────────────────────────────

    async def act(self, space: Any, action: Any) -> str:
        """Apply one action. Failures are OBSERVATIONS (a note for the history), never
        exceptions that end the run. Raises SessionLost only when a dead session cannot be
        brought back."""
        self.act_timed_out = False
        try:
            return await self.apply(space, action)
        except DriverTimeout as exc:
            # The action itself hung. Issue NO further command here: the session is
            # one-command-at-a-time, so anything sent now queues behind the hung call and
            # burns its own full timeout. The next read decides whether to reconnect.
            logger.warning("action timed out (%s); recovering", exc)
            self.act_timed_out = True
            return (f"[step] {action.action} — the app did not respond in time; "
                    "recovered and continuing (do not assume this action took effect)")
        except DriverSessionDead as exc:
            # The action crashed the app / killed the WDA session. Reconnect (capped) and
            # relaunch; if the budget's spent, end the run with its partial evidence.
            logger.warning("action hit a dead session (%s)", exc)
            if not await self._reconnect():
                raise SessionLost() from exc
            return ("[step] the app crashed and was relaunched after this action; "
                    "re-orient from the current screen (earlier progress may be lost)")
        except Exception as exc:
            # Actuation failed (e.g. `type` on a non-editable element): a dead end for THIS
            # action, not the run.
            logger.warning("action failed (%s); noting and continuing", exc)
            return (f"[step] {action.action} {getattr(action, 'ref', '')} — could not be "
                    f"performed ({exc}); it did not take effect, try a different element "
                    "or approach")

    async def _scroll_to(self, space: Any, action: ScrollTo) -> str:
        """Swipe until an element containing the text shows, the list stops moving (its end),
        or SCROLL_TO_MAX swipes — one action, no decision per swipe (reaching a row
        hundreds deep took 30 decided swipes and still fell short)."""
        want = (action.text or "").strip().lower()
        if not want:
            return "scroll_to needs the text to look for"

        def shows(sp: Any) -> bool:
            return any(_names(want, (e.label or "").strip().lower()) for e in sp.elements)

        def row_of(sp: Any, label: str) -> Any:
            return next((e for e in sp.elements if e.label == label), None)

        if shows(space):
            return f'"{action.text}" is already on screen (nothing scrolled)'
        anchor = space.by_ref(action.ref) if (action.ref or "").strip() else None
        cur, still, n = space, 0, 0
        direction, turned, seen = action.direction, "", None
        while n < SCROLL_TO_MAX:
            await self.driver.swipe(direction, row_of(cur, anchor.label) if anchor else None)
            n += 1
            # A virtualized list re-renders its rows a moment AFTER the swipe; reading at once
            # saw the old rows and called the list's end after 2 swipes.
            await asyncio.sleep(SCROLL_SETTLE_S)
            prev, cur = cur, await self.perceiver.observe(self.driver)
            if cur is None:
                break
            if shows(cur):
                return f'scrolled {direction} {n} time(s){turned} until "{action.text}" appeared'
            if screen_moved(prev, cur):
                still, seen = 0, None
            else:
                # The tree did not change: the list's end — or a list the tree no longer shows
                # (rows past a list's 100th child never enter the tree, while the pixels
                # keep scrolling). Read the screen itself before calling it the end.
                lines = await asyncio.to_thread(ocr_lines, await self.screenshot())
                if any(_names(want, ln.strip().lower()) for ln in lines):
                    return (f'scrolled {direction} {n} time(s){turned} until "{action.text}" '
                            "appeared on screen (read from the pixels; the list's rows are not "
                            "in the accessibility tree)")
                text = {ln.strip() for ln in lines if ln.strip()}
                if seen is not None and _scrolled(seen, text):
                    still = 0
                else:
                    still += 1
                seen = text
            if still >= 3:
                if not turned:
                    # One end reached and no item: it may lie the other way — the list can open
                    # part-way down, or "down" was asked (it swipes toward the TOP, an
                    # easy slip). Search the other way once, within the same swipe budget.
                    direction, still, turned = _OPPOSITE[direction], 0, f" (reached the end going {direction}, so turned {_OPPOSITE[direction]})"
                    continue
                return (f'scrolled {direction} {n} time(s){turned}: the list stopped moving (its '
                        f'end) and "{action.text}" never appeared')
        return f'scrolled {direction} {n} time(s){turned}; "{action.text}" has not appeared yet'

    async def _retap_if_it_only_closed_the_keyboard(self, before: Any, element: Any) -> bool:
        """Many apps (React Native lists by default) spend the first tap while a keyboard is up
        on closing it: the tap reaches nothing (a search result needed two taps, and an
        agent that tapped once called a correct app broken). When the keyboard went away and
        NOTHING else changed (no element appeared or changed state, the target is still
        there), tap once more. A real effect always adds or changes something, so a toggle,
        a selection or a deletion is never tapped twice."""
        await asyncio.sleep(KEYBOARD_CLOSE_S)
        after = await self.perceiver.observe(self.driver)
        if after is None or after.keyboard:
            return False
        if not set(map(_state, after.elements)) <= set(map(_state, before.elements)):
            return False
        again = next((e for e in after.elements
                      if e.type == element.type and e.label == element.label), None)
        if again is None:
            return False
        await self.driver.tap(again)
        return True

    async def _hide_keyboard(self) -> str:
        """Put the keyboard away, and CHECK that it went: hideKeyboard only presses Done/Dismiss
        keys and reports success on a keyboard that has none (an onboarding form's
        keyboard stayed up through 8 steps of 'hid the keyboard'). The fallbacks are what the
        agent itself found to work: tap plain text above the keyboard (a heading: it does
        nothing, but it takes focus away from the field), then a one-line field's return key."""
        await self.driver.hide_keyboard()
        ok, after = await self._keyboard_gone()
        if ok:
            return "hid the keyboard"
        if after is None:
            return "could not hide the keyboard (the screen could not be read)"
        last = after
        spot = blank_spot(after)
        if spot is not None:
            await self.driver.tap(spot)
            hidden, again = await self._keyboard_gone()
            if hidden:
                what = spot.label if not spot.ref else f'the plain text "{spot.label}"'
                return f"hid the keyboard (it has no Done key; tapped {what})"
            last = again or after
        ret = next((e for e in last.elements if (e.label or "").strip().lower() == "return"), None)
        if ret is not None and _credential_keyboard(last):
            # iOS shows the "Passwords" AutoFill key only for a credential field (password /
            # username), and those are single-line: "return" cannot add a newline there, it puts
            # the keyboard away (on a flattened sign-in form the agent tapped a SIGN IN the
            # keyboard covered 15 times and never pressed return itself).
            await self.driver.tap(ret)
            hidden, _ = await self._keyboard_gone()
            if hidden:
                return ("hid the keyboard by pressing its return key (the field is a one-line "
                        "password/username field; on a form, return may also submit it — check "
                        "the screen)")
        tip = (f'tap the keyboard\'s "return" key ({ret.ref}) — on a one-line field it puts the '
               "keyboard away (on a form it may also submit it)" if ret is not None else
               "scroll the page, or use the keyboard's own return key if it is safe")
        return f"could not hide the keyboard (no Done/Dismiss key, and tapping the screen did not move it) — {tip}"

    async def _keyboard_gone(self) -> tuple[bool, Any]:
        """Whether the keyboard is down, confirmed by two reads: one read can catch it
        mid-animation and see it gone while it settles back ('hid the keyboard', and it
        was still up next step). Returns (gone, the latest screen read)."""
        await asyncio.sleep(KEYBOARD_CLOSE_S)
        sp = await self.perceiver.observe(self.driver)
        if sp is None or sp.keyboard:
            return False, sp
        await asyncio.sleep(KEYBOARD_RECHECK_S)
        again = await self.perceiver.observe(self.driver)
        return (again is None or not again.keyboard), (again or sp)

    async def _tap_the_rows_own_control(self, element: Any) -> str:
        """A two-state control merged with its label into one wide row (a Flutter CheckboxListTile-
        style row, a UIKit settings cell): its centre is the LABEL, which often does nothing
        (a Flutter terms checkbox stayed off through 10 centre taps and the agent failed a
        correct app). If the value did not change, tap where the control itself sits — the
        trailing edge (iOS switches), then the leading edge (checkboxes) — stopping as soon as it
        flips, so it is never flipped twice."""
        async def state() -> Any:
            await asyncio.sleep(KEYBOARD_CLOSE_S)
            sp = await self.perceiver.observe(self.driver)
            same = next((e for e in (sp.elements if sp else [])
                         if e.type == element.type and e.label == element.label), None)
            return same.toggled if same is not None else None
        if await state() != element.toggled:
            return ""
        f = element.frame
        cy = int(f.y + f.height / 2)
        for side, x in (("right", f.x + f.width - f.height / 2), ("left", f.x + f.height / 2)):
            await self.driver.tap_point(int(x), cy)
            now = await state()
            if now is not None and now != element.toggled:
                return (f" (the row's centre did not change it; its control at the {side} edge "
                        "was tapped)")
        return ""

    async def _type_under_label(self, space: Any, label: Any, text: str, clear: bool = False) -> str | None:
        """Typing into a field LABEL: on a form whose fields are hidden from the tree (a merged /
        flattened view) the agent sees "First name" (tree or OCR) but no field. Find the input
        box drawn under the label, tap it, type there (estimated tap_points kept missing
        the 44 pt-tall box). None when no box is found — the caller types as before."""
        box = input_box_below(await self.screenshot(), label.frame, space.screen_w)
        if box is None:
            return None
        cx, cy = box.center
        if space.keyboard and space.keyboard_top and cy >= space.keyboard_top:
            hidden = await self._hide_keyboard()
            return (f'[step] the field under "{label.label}" is under the keyboard, so nothing was '
                    f"typed. {hidden[:1].upper()}{hidden[1:]}; type into it again")
        await self.driver.tap_point(int(cx), int(cy))
        await asyncio.sleep(KEYBOARD_CLOSE_S)
        await self.driver.type_focused(text, clear=clear)
        note = f'[step] typed into the field under "{label.label}" (found by its outline): "{text}"'
        if _SECRET_LABEL.search(label.label or ""):
            # iOS keeps a password field's text AND its keyboard out of screenshots: the box looks
            # empty, and controls the keyboard really covers look free (the agent failed a
            # correct app, then tapped at a covered button for 20 steps). Put the keyboard away
            # now, so the screenshot matches the screen again.
            hidden = await self._hide_keyboard()
            note += (" — it now holds what you typed, though screenshots never show a password "
                     "field's text, so the box LOOKS empty. Do not retype it (that adds the text a "
                     "second time); go on to the next step — the app's response to submitting is "
                     f"the real check. Then: {hidden}")
        return note

    async def apply(self, space: Any, action: Any) -> str:
        """Execute one action, no recovery; return a history note with its effect."""
        handler = _APPLY.get(type(action))
        if handler is None:
            return f"[step] {action.action}"
        return await handler(self, space, action)

    # ── one handler per action: each returns the step's history note ──

    async def _apply_tap(self, space: Any, action: Tap) -> str:
        d = self.driver
        element = space.by_ref(action.ref)
        if element is None:
            return f"[step] {action.action} {action.ref} — ref not found, skipped"
        if element.covered_by == KEYBOARD:
            # A tap there would land on a key and type a letter. Put the keyboard away
            # instead; the element's position changes, so the agent taps it afresh.
            hidden = await d.hide_keyboard()
            return (f'[step] {action.ref} "{element.label}" was under the keyboard — '
                    + ("the keyboard was hidden; nothing was tapped: tap it again on "
                       "the current screen" if hidden else "the keyboard could not be "
                       "hidden (no Done/Dismiss key); nothing was tapped"))
        await d.tap(element)
        verb = f'tapped {action.ref} "{element.label}"'
        if space.keyboard and await self._retap_if_it_only_closed_the_keyboard(space, element):
            verb += " (the first tap only closed the keyboard, so it was tapped again)"
        elif element.toggled is not None and element.frame.width >= 3 * element.frame.height:
            verb += await self._tap_the_rows_own_control(element)
        return f"[step] {verb}"

    async def _apply_type(self, space: Any, action: Type) -> str:
        d = self.driver
        if not (action.ref or "").strip():
            # Ref-less type: enter text into the currently-focused field.
            await d.type_focused(action.text, clear=action.clear)
            return f'[step] typed into focused field: "{action.text}"'
        element = space.by_ref(action.ref)
        if element is None:
            return f"[step] {action.action} {action.ref} — ref not found, skipped"
        if element.type not in TEXT_INPUT_TYPES:
            typed = await self._type_under_label(space, element, action.text, action.clear)
            if typed is not None:
                return typed
        if action.clear:
            await d.type(element, action.text, clear=True)
        else:
            await d.type(element, action.text)
        return (f'[step] {"replaced" if action.clear else "typed into"} {action.ref}: '
                f'"{action.text}"')

    async def _apply_swipe(self, space: Any, action: Swipe) -> str:
        target = space.by_ref(action.ref) if (action.ref or "").strip() else None
        await self.driver.swipe(action.direction, target)
        return (f'[step] swiped {action.direction} on {action.ref} "{target.label}"'
                if target else f"[step] swiped {action.direction}")

    async def _apply_wait(self, space: Any, action: Wait) -> str:
        secs = min(max(action.seconds, 0.5), MAX_WAIT_S)
        await asyncio.sleep(secs)
        return f"[step] waited {secs:g}s for the app"

    async def _apply_back(self, space: Any, action: Back) -> str:
        control = back_control(space)
        if control is not None:
            await self.driver.tap(control)
            return f'[step] went back via "{control.label}"'
        await self.driver.back()
        return "[step] went back (system edge-swipe)"

    async def _apply_drag(self, space: Any, action: Drag) -> str:
        src = space.by_ref(action.ref)
        dst = space.by_ref(action.to_ref) if (action.to_ref or "").strip() else None
        if src is None:
            return f"[step] drag {action.ref} — ref not found, skipped"
        if dst is None and (action.to_x is None or action.to_y is None):
            return "[step] drag needs a target: to_ref, or to_x and to_y (fractions 0-1)"
        if dst is None and not (0 <= action.to_x <= 1 and 0 <= action.to_y <= 1):
            return "[step] drag target was NOT used: to_x and to_y are fractions between 0 and 1"
        xy = None if dst is not None else (action.to_x * space.screen_w, action.to_y * space.screen_h)
        await self.driver.drag(src, dst, xy)
        return (f'[step] dragged {action.ref} "{src.label}" onto {action.to_ref} "{dst.label}"' if dst
                else f'[step] dragged {action.ref} "{src.label}" to ({action.to_x:.2f},{action.to_y:.2f})')

    async def _apply_scroll_to(self, space: Any, action: ScrollTo) -> str:
        return f"[step] {await self._scroll_to(space, action)}"

    async def _apply_long_press(self, space: Any, action: LongPress) -> str:
        element = space.by_ref(action.ref)
        if element is None:
            return f"[step] {action.action} {action.ref} — ref not found, skipped"
        secs = min(max(action.seconds, 0.3), 5.0)
        await self.driver.long_press(element, secs)
        return f'[step] long-pressed {action.ref} "{element.label}" for {secs:g}s'

    async def _apply_double_tap(self, space: Any, action: DoubleTap) -> str:
        element = space.by_ref(action.ref)
        if element is None:
            return f"[step] {action.action} {action.ref} — ref not found, skipped"
        await self.driver.double_tap(element)
        return f'[step] double-tapped {action.ref} "{element.label}"'

    async def _apply_hide_keyboard(self, space: Any, action: HideKeyboard) -> str:
        return f"[step] {await self._hide_keyboard()}"

    async def _apply_background(self, space: Any, action: BackgroundApp) -> str:
        await self.driver.background_app(action.seconds)
        return f"[step] sent the app to the background for {action.seconds}s and reopened it"

    async def _apply_tap_point(self, space: Any, action: TapPoint) -> str:
        # Fractions (0..1) → points. An estimate OFF the screen is a misread (a pixel value,
        # a guessed image size): clamping it tapped the screen edge (often the tab bar) and
        # sent the agent somewhere it never meant to go. Refuse it and say why.
        if not (0.0 <= action.x <= 1.0 and 0.0 <= action.y <= 1.0):
            return (f"[step] tap_point ({action.x:g},{action.y:g}) was NOT performed: x and "
                    "y are fractions of the screen between 0 and 1 — read them off the "
                    "grid lines labelled .1 … .9 in the screenshot")
        fx, fy = action.x, action.y
        px = int(fx * space.screen_w)
        py = int(fy * space.screen_h)
        if not space.keyboard and py >= space.screen_h * KEYBOARD_ZONE_FRAC:
            # The step's read can miss a keyboard that is still settling right after typing
            # (it read "no keyboard", the tap typed a letter into the password, and the
            # login said "Invalid credentials"). A tap where a keyboard could be: read again.
            fresh = await self.perceiver.observe(self.driver)
            if fresh is not None and fresh.keyboard:
                space = fresh
        if space.keyboard and space.keyboard_top and py >= space.keyboard_top:
            # That point is ON the keyboard: the tap would type a letter into the field
            #. Put
            # the keyboard away instead; what was under it moves, so the agent looks again.
            hidden = await self._hide_keyboard()
            return (f"[step] tap_point ({fx:.2f},{fy:.2f}) was NOT performed: that point is on "
                    "the keyboard, which is UP even if the screenshot does not show it (iOS hides "
                    "the keyboard from screenshots while a password is entered). "
                    f"{hidden[:1].upper()}{hidden[1:]}; look at the screen again before tapping")
        await self.driver.tap_point(px, py)
        return f"[step] tapped point ({fx:.2f},{fy:.2f}) → ({px},{py}) [no-ref coordinate tap]"


# How each action is applied (DeviceSession.apply). A new action is one handler + one entry.
_APPLY = {
    Tap: DeviceSession._apply_tap, Type: DeviceSession._apply_type,
    Swipe: DeviceSession._apply_swipe, Wait: DeviceSession._apply_wait,
    Back: DeviceSession._apply_back, Drag: DeviceSession._apply_drag,
    ScrollTo: DeviceSession._apply_scroll_to, LongPress: DeviceSession._apply_long_press,
    DoubleTap: DeviceSession._apply_double_tap, HideKeyboard: DeviceSession._apply_hide_keyboard,
    BackgroundApp: DeviceSession._apply_background, TapPoint: DeviceSession._apply_tap_point,
}
