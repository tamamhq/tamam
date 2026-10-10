"""Replay a compiled script on a live device — no LLM for the steps, checks evaluated in code.

Outcomes (what the caller does with them):
  passed  — every step resolved, every locked check held, every unchecked clause was confirmed
            by the AI look → the run PASSES, fast.
  failed  — a locked check failed ON THE SCREEN IT BELONGS TO (its anchors are there) → an app
            bug → the run FAILS. Only code can fail a run; an AI look never does.
  miss    — anything that means "the script no longer fits": target not found or ambiguous,
            expected screen not reached, an action that changed nothing, a check failing on a
            different screen, an AI look that is unsure or disagrees → a full AI run decides.
  error   — the device itself failed (session lost) → a full AI run decides.

Fail closed: anything that cannot be evaluated is a miss, never a pass.
"""

from __future__ import annotations

import asyncio
import re
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from tamam.executors.core.actions import (Back, BackgroundApp, DoubleTap, HideKeyboard, LongPress, Swipe, Tap,
                                        TapPoint, Type)
from tamam.executors.core.contracts import ActingSession, SessionLost, screen_moved
from tamam.graduation.compiler import credential_values
from tamam.executors.core.screen import element_shows, screen_shows, screen_texts
from tamam.graduation.resolver import resolve
from tamam.graduation.script import Check, ClauseCoverage, CompiledScript, Step

_OPPOSITE = {"up": "down", "down": "up", "left": "right", "right": "left"}

logger = logging.getLogger(__name__)

POLL_S = 1.0          # between re-reads while waiting for the app
SCREEN_WAIT_READS = 4  # reads to wait for an expected screen / an effect (~4 s)
CHECK_WAIT_READS = 4   # reads a visible check may wait for the app (~4 s)
LAUNCH_WAIT_S = 20.0   # the FIRST screen: a fresh device (a farm phone) can take this long to launch the app
ABSENT_HOLD_S = 1.5    # an absence must still hold this long after (a late error, a slow list)
AI_LOOKS = 2           # looks at one clause before escalating (the 2nd after a pause)
AI_RELOOK_S = 3.0


async def _noop() -> None:
    return None


@dataclass
class CheckResult:
    id: str
    clause: str
    how: str           # "code" | "ai"
    ok: bool
    detail: str = ""


@dataclass
class ReplayOutcome:
    verdict: str                      # passed | failed | miss | error
    reason: str
    at_step: str = ""
    checks: list[CheckResult] = field(default_factory=list)
    steps_done: int = 0
    seconds: float = 0.0
    cancelled: bool = False           # a guardrail ended the run during replay
    seen: list[str] = field(default_factory=list)   # on a miss: what the screen showed instead
    notes: list[str] = field(default_factory=list)  # adaptations: skipped / declined prompts

    def as_dict(self) -> dict:
        return {"verdict": self.verdict, "reason": self.reason, "at_step": self.at_step,
                "steps_done": self.steps_done, "seconds": round(self.seconds, 1), "seen": self.seen,
                "notes": self.notes,
                "checks": [c.__dict__ for c in self.checks]}


# An AI look at one clause: (clause, space, screenshot path) → "holds" | "fails" | "unsure".
ClauseLook = Callable[[str, Any, str | None], Awaitable[tuple[str, str]]]


def _anchors_match(anchors: list[str], space: Any) -> bool:
    """On the expected screen: at least two of its anchors (or all, when fewer) are shown."""
    if not anchors:
        return True
    texts, _ = screen_texts(space.elements)
    hits = sum(screen_shows(texts, a) for a in anchors)
    return hits >= min(2, len(anchors))


def check_holds(check: Check, space: Any) -> bool:
    """A locked check, in code, against one screen read."""
    texts, truncated = screen_texts(space.elements)
    if check.kind == "visible":
        return screen_shows(texts, check.text, check.with_text)
    if check.kind == "absent":
        return not truncated and not screen_shows(texts, check.text)
    if check.kind == "toggled":
        hits = [e for e in space.elements if e.label == check.text and e.toggled is not None]
        return len(hits) == 1 and hits[0].toggled is check.expected
    if check.kind == "selected":
        return any(e.selected and element_shows(e.label, check.text) for e in space.elements)
    return False


class ReplayRunner:
    def __init__(self, session: ActingSession, script: CompiledScript, test_text: str,
                 clause_look: ClauseLook | None = None, heartbeat=_noop,
                 on_step: Callable[[Step, Any, str, str | None], Awaitable[None]] | None = None,
                 sleep=asyncio.sleep,
                 on_look: Callable[[str, str, str, str | None], Awaitable[None]] | None = None) -> None:
        self.s = session
        self.script = script
        self.creds = credential_values(test_text)
        self.look = clause_look
        self.heartbeat = heartbeat
        self.on_step = on_step
        self.sleep = sleep
        self.on_look = on_look
        self.results: list[CheckResult] = []
        self.notes: list[str] = []      # what replay adapted on this device (skips, declines)
        self._declines = 0

    async def run(self) -> ReplayOutcome:
        t0 = time.monotonic()
        self._last_space = None
        try:
            out = await self._run()
        except SessionLost:
            out = ReplayOutcome("error", "The device session was lost during replay.")
        if out.verdict in ("miss", "failed") and self._last_space is not None:
            out.seen = screen_texts(self._last_space.elements)[0][:12]
        out.checks = self.results
        out.notes = list(self.notes)
        out.seconds = time.monotonic() - t0
        return out

    async def _read(self) -> Any:
        space = await self.s.observe(self.heartbeat)
        await self.heartbeat()
        if space is not None:
            self._last_space = space
        return space

    async def _run(self) -> ReplayOutcome:
        space = await self._read()
        if space is None:
            return ReplayOutcome("error", "The device did not respond.")
        bad = await self._evaluate_at("", space)
        if bad:
            return bad
        steps = self.script.steps
        for i, step in enumerate(steps):
            nxt = steps[i + 1] if i + 1 < len(steps) else None
            skip_to = nxt.pre if (step.optional and nxt is not None and nxt.pre) else None
            space, bad = await self._wait_for(step.pre, space, step, first=(i == 0), skip_to=skip_to)
            if bad:
                bad.steps_done = i
                return bad
            if skip_to and not _anchors_match(step.pre, space):
                # Its first-run prompt did not appear on this device; the next screen did.
                self.notes.append(f'skipped {_where(step, "step")}: its prompt did not appear')
            else:
                space, bad = await self._do(step, space)
                if bad:
                    bad.steps_done = i
                    return bad
            bad = await self._evaluate_at(step.id, space)
            if bad:
                bad.steps_done = i + 1
                return bad
        return ReplayOutcome("passed", self._summary(), steps_done=len(self.script.steps))

    async def _decline_prompt(self, space: Any) -> Any | None:
        """An UNRECORDED first-run / marketing prompt is in the way (fresh devices show them or
        not, run to run,): press its DECLINE answer ("Maybe Later", "Not Now") — it changes
        no data — and return the new screen. At most twice a run, and noted in the report."""
        if space is None or self._declines >= 2:
            return None
        from tamam.graduation.compiler import PROMPT_DECLINES
        button = next((e for e in space.elements if e.type == "Button"
                       and (e.label or "").strip().lower() in PROMPT_DECLINES), None)
        if button is None:
            return None
        self._declines += 1
        await self.s.act(space, Tap(ref=button.ref))
        self.notes.append(f'declined an unrecorded prompt with "{button.label}"')
        await self.sleep(POLL_S)
        return await self._read()

    def _summary(self) -> str:
        code = sum(1 for r in self.results if r.how == "code")
        ai = sum(1 for r in self.results if r.how == "ai")
        return f"Fast replay passed: {code} checked by code, {ai} by AI on the screen."

    async def _wait_for(self, anchors: list[str], space: Any, step: Step,
                        first: bool = False, skip_to: list[str] | None = None,
                        ) -> tuple[Any, ReplayOutcome | None]:
        # The first screen waits for the app to launch (by time: reads are slow on a farm);
        # later screens get the short wait between steps.
        deadline = time.monotonic() + LAUNCH_WAIT_S if first else None
        reads = 0
        while True:
            if _anchors_match(anchors, space):
                return space, None
            if skip_to and _anchors_match(skip_to, space):
                return space, None           # an optional prompt that did not appear (caller skips)
            declined = await self._decline_prompt(space)
            if declined is not None:
                space = declined
                continue
            reads += 1
            if (deadline is None and reads >= SCREEN_WAIT_READS) or \
                    (deadline is not None and time.monotonic() >= deadline):
                break
            await self.sleep(POLL_S)
            space = await self._read()
            if space is None:
                return space, ReplayOutcome("error", "The device did not respond.", step.id)
        return space, ReplayOutcome(
            "miss", f'Before {_where(step, "step")}: the expected screen ({", ".join(anchors)}) did not appear.',
            step.id)

    async def _do(self, step: Step, space: Any) -> tuple[Any, ReplayOutcome | None]:
        before = space.source_hash
        if step.do == "scroll_until":
            return await self._scroll(step, space)
        action, why = self._action(step, space)
        if action is None:
            return space, ReplayOutcome("miss", f'{_where(step)}: {why}', step.id)
        note = await self.s.act(space, action)
        if self.s.act_timed_out or "could not be performed" in note or "not found" in note:
            return space, ReplayOutcome("miss", f'{_where(step)} did not go through ({note}).', step.id)
        shot = None
        if step.do in ("background", "hide_keyboard"):
            # Leaving and reopening the app SHOULD bring back the same screen; hiding a keyboard
            # that is already down (replay types without one staying up) changes nothing either.
            space = await self._read()
            if space is None:
                return space, ReplayOutcome("error", "The device did not respond.", step.id)
            if self.on_step is not None:
                await self.on_step(step, space, note, await self.s.screenshot())
            return space, None
        for _ in range(SCREEN_WAIT_READS):
            await self.sleep(POLL_S)
            space = await self._read()
            if space is None:
                return space, ReplayOutcome("error", "The device did not respond.", step.id)
            if space.source_hash != before:
                break
        else:
            # A tap by position that changed nothing is fine ONLY when the screen this step
            # led to is already showing: it was redundant here (a tap that dismissed a keyboard
            # replay never raised). Any other no-effect step means the script no longer fits.
            if not (step.do == "tap_point" and step.post and _anchors_match(step.post, space)):
                return space, ReplayOutcome("miss", f'{_where(step)} changed nothing on screen.',
                                            step.id)
        if self.on_step is not None:
            shot = await self.s.screenshot()
            await self.on_step(step, space, note, shot)
        return space, None

    def _action(self, step: Step, space: Any) -> tuple[Any, str]:
        if step.do in ("long_press", "double_tap") and step.target is not None:
            r = resolve(step.target, space)
            if r.element is None:
                return None, _not_found(step.target, space, r)
            if step.do == "long_press":
                return LongPress(ref=r.element.ref, seconds=step.seconds or 1.0), ""
            return DoubleTap(ref=r.element.ref), ""
        if step.do in ("tap", "type"):
            if step.target is None:
                if step.do == "type":
                    return Type(ref="", text=self._text(step), clear=step.clear), ""
                return None, "no target recorded"
            r = resolve(step.target, space)
            if r.element is None:
                return None, _not_found(step.target, space, r)
            if step.do == "tap":
                return Tap(ref=r.element.ref), ""
            return Type(ref=r.element.ref, text=self._text(step), clear=step.clear), ""
        if step.do == "back":
            return Back(), ""
        if step.do == "background":
            return BackgroundApp(seconds=step.seconds or 3), ""
        if step.do == "hide_keyboard":
            return HideKeyboard(), ""
        if step.do == "tap_point" and step.point and len(step.point) == 2:
            size = [getattr(space, "screen_w", 0), getattr(space, "screen_h", 0)]
            if self.script.device_class and list(self.script.device_class) != size:
                return None, "a tap by position recorded on a different screen size"
            return TapPoint(x=step.point[0], y=step.point[1]), ""
        return None, f"cannot replay a {step.do!r} step"

    def _text(self, step: Step) -> str:
        t = step.text
        if t is None:
            return ""
        if t.credential is not None and 0 <= t.credential < len(self.creds):
            return self.creds[t.credential]
        return t.literal or ""

    async def _scroll(self, step: Step, space: Any) -> tuple[Any, ReplayOutcome | None]:
        def found(sp: Any) -> bool:
            if step.target is not None:
                return resolve(step.target, sp).element is not None
            if step.until_text:
                return screen_shows(screen_texts(sp.elements)[0], step.until_text, step.until_with)
            return False

        direction = step.direction or "up"

        def swipe(sp: Any) -> Swipe:
            on = resolve(step.scroll_on, sp).element if step.scroll_on is not None else None
            return Swipe(direction=direction, ref=on.ref if on is not None else "")

        swipes = step.max or 1
        if step.target is None and not step.until_text:
            for _ in range(swipes):           # a fixed scroll the recording made
                await self.s.act(space, swipe(space))
                space = await self._read()
                if space is None:
                    return space, ReplayOutcome("error", "The device did not respond.", step.id)
            return space, None
        # Searching: stop at a list's end instead of spending every swipe, and search the other
        # way once (the list can open part-way down; the recording may name the direction the
        # agent first tried, not the one that found the item).
        still, turned, n = 0, False, 0
        for _ in range(swipes + 1):
            if found(space):
                return space, None
            await self.s.act(space, swipe(space))
            n += 1
            prev, space = space, await self._read()
            if space is None:
                return space, ReplayOutcome("error", "The device did not respond.", step.id)
            still = 0 if screen_moved(prev, space) else still + 1
            if still >= 3:
                if turned:
                    break
                # One end and no item: it may lie the other way (the list can open part-way).
                direction, still, turned = _OPPOSITE[direction], 0, True
        return space, ReplayOutcome("miss", f'{_where(step)}: not found after {n} swipes.', step.id)

    async def _evaluate_at(self, after: str, space: Any) -> ReplayOutcome | None:
        checks = [c for c in self.script.checks if c.after == after]
        # Positive checks first: an ABSENCE is only meaningful on the read where the screen's
        # positive content is there (a list still loading shows nothing, absent included).
        for check in sorted(checks, key=lambda c: c.kind == "absent"):
            bad, space = await self._code_check(check, space)
            if bad:
                bad.at_step = after
                return bad
        for cov in [c for c in self.script.coverage
                    if c.after == after and c.kind == "verification" and not c.checked_by_code]:
            bad = await self._ai_check(cov, space)
            if bad:
                bad.at_step = after
                return bad
        return None

    def _gate(self, anchors: list[str], space: Any) -> str | None:
        """Why a check may not be judged on this read, or None. Fail closed: only on the
        recorded screen, and never under an app dialog the recording did not have."""
        if space is None:
            return "the screen could not be read"
        alert = getattr(space, "alert", "") or ""
        if alert and not any(element_shows(alert, a) for a in anchors):
            return f'an app dialog is showing ("{alert[:80]}")'
        if anchors and not _anchors_match(anchors, space):
            return "the screen differs from the recorded one"
        return None

    async def _code_check(self, check: Check, space: Any) -> tuple[ReplayOutcome | None, Any]:
        reads = CHECK_WAIT_READS if check.kind != "absent" else 1
        blocked = None
        for i in range(reads):
            blocked = self._gate(check.on_screen, space)
            if blocked is None and check_holds(check, space):
                if check.kind == "absent":
                    await self.sleep(ABSENT_HOLD_S)          # a late error / slow list
                    space = await self._read()
                    if self._gate(check.on_screen, space) or not check_holds(check, space):
                        break
                self.results.append(CheckResult(check.id, check.clause, "code", True))
                return None, space
            if i < reads - 1:
                await self.sleep(POLL_S)
                space = await self._read()
                if space is None:
                    return ReplayOutcome("error", "The device did not respond."), space
        detail = _describe(check, space)
        self.results.append(CheckResult(check.id, check.clause, "code", False, detail))
        # FAILED (an app bug) only on the recorded screen, with no unexpected dialog, and only
        # when the check knows which screen it belongs to; anything else is a miss.
        if check.on_screen and self._gate(check.on_screen, space) is None:
            return ReplayOutcome("failed", f'"{check.clause}": {detail}'), space
        why = self._gate(check.on_screen, space) or "it has no recorded screen to compare"
        return ReplayOutcome("miss", f'"{check.clause}" could not be judged: {why} ({detail}).'), space

    def _not_evidence(self, cov: ClauseCoverage) -> list[str]:
        """Texts a look may not cite as proof: what names the screen rather than shows its
        content — any label the script tapped (a page's title mirrors the row that opened it)
        and short anchors (titles, tabs, back buttons). A review sentence that happened to be
        recorded as an anchor is still content, and still evidence."""
        tapped = [s.target.label for s in self.script.steps if s.target is not None and s.target.label]
        return tapped + [a for a in cov.on_screen if len(a.split()) <= 3]

    async def _ai_check(self, cov: ClauseCoverage, space: Any) -> ReplayOutcome | None:
        if self.look is None:
            return ReplayOutcome("miss", f'"{cov.clause}" needs an AI look, which is off.')
        if not cov.on_screen:
            return ReplayOutcome("miss", f'"{cov.clause}" has no recorded screen to look at.')
        blocked = self._gate(cov.on_screen, space)
        if blocked:
            return ReplayOutcome("miss", f'"{cov.clause}": {blocked}.')
        verdict, why, shot = "unsure", "", None
        for attempt in range(AI_LOOKS):
            if attempt:
                await self.sleep(AI_RELOOK_S)      # a slow screen (still loading) gets one more look
                space = await self._read()
                if space is None or self._gate(cov.on_screen, space):
                    break
            shot = await self.s.screenshot()
            try:
                verdict, why = await self.look(cov.clause, space, shot)
            except Exception as exc:  # the look failing is not evidence either way
                logger.warning("AI clause look failed: %s", exc)
                verdict, why = "unsure", "the AI look failed"
            if verdict != "unsure":
                break                              # only "unsure" earns a second look, never "fails"
        if verdict == "holds" and not quotes_on_screen(why, space, exclude=self._not_evidence(cov)):
            # The screenshot is newer than `space`: content that arrived in between (a list
            # finishing loading) is in the picture but not in the text read. Read once more —
            # the evidence must still be real, current screen text.
            fresh = await self._read()
            if fresh is not None and not self._gate(cov.on_screen, fresh):
                space = fresh
        if verdict == "holds" and not quotes_on_screen(why, space, exclude=self._not_evidence(cov)):
            # A "holds" must point at specific text this screen really shows — not a guess, and
            # not just the screen's title.
            verdict, why = "unsure", f"its evidence is not on the screen ({why[:120]})"
        ok = verdict == "holds"
        self.results.append(CheckResult(cov.clause_id, cov.clause, "ai", ok, why))
        if self.on_look is not None:
            await self.on_look(cov.clause, verdict, why, shot)
        if ok:
            return None
        # An AI glance never fails a run on its own: a "fails" or "unsure" escalates to a full run.
        return ReplayOutcome("miss", f'"{cov.clause}": the AI look said {verdict} ({why}).')


_QUOTE = re.compile(r'"([^"]{4,120})"')


def quotes_on_screen(evidence: str, space: Any, exclude: list[str] | None = None) -> bool:
    """The AI look's evidence quotes at least one specific text this screen really shows (a
    whole-token match in one element), other than the screen's own anchors (its title)."""
    if space is None:
        return False
    skip = {a.lower() for a in (exclude or [])}
    labels = screen_texts(space.elements)[0]
    for q in _QUOTE.findall(evidence or ""):
        q = q.strip()
        if q.lower() in skip:
            continue
        if any(element_shows(lbl, q) for lbl in labels):
            return True
    return False


def _where(step: Step, word: str = "Step") -> str:
    """'Step 2 (Tap "Orders")' — a step named the way the run page numbers it."""
    n = step.id[1:] if step.id[:1] == "s" and step.id[1:].isdigit() else step.id
    return f"{word} {n} ({step.say})"


# Accessibility types in the words a tester uses.
_KIND = {"StaticText": "plain text", "Button": "button", "Cell": "row", "Link": "link",
         "Switch": "switch", "TextField": "text field", "Image": "image", "Other": "container"}


def _not_found(target: Any, space: Any, r: Any) -> str:
    """Why a step's target could not be used, in words a person can act on."""
    label = target.label
    if r.how != "none":
        return f'"{label}" matches {r.candidates} elements'
    same = [e for e in getattr(space, "elements", []) if (e.label or "") == label and label]
    if same:
        # The label is shown, but not by the kind of control that was tapped — often the
        # very bug (a dead row rendered as plain text). Stated as what is seen, not a cause:
        # the text may also be another element (a title) that happens to share the label.
        kinds = sorted({_KIND.get(e.type, e.type) for e in same})
        return (f'no "{label}" {_KIND.get(target.type, target.type)} on the screen; '
                f'"{label}" appears only as {" / ".join(kinds)}')
    return f'"{label}" is not on the screen'


def _describe(check: Check, space: Any) -> str:
    if space is None:
        return "the screen could not be read"
    if check.kind == "visible":
        what = f'"{check.text}"' + (f' with "{check.with_text}"' if check.with_text else "")
        return f"expected {what} on screen; not found"
    if check.kind == "absent":
        return f'expected "{check.text}" NOT to be on screen; it is there'
    if check.kind == "toggled":
        hits = [e for e in space.elements if e.label == check.text and e.toggled is not None]
        now = ("on" if hits[0].toggled else "off") if len(hits) == 1 else "not found"
        return f'expected the "{check.text}" switch {"on" if check.expected else "off"}; it is {now}'
    if check.kind == "selected":
        return f'expected "{check.text}" to be selected; it is not'
    return "check failed"
