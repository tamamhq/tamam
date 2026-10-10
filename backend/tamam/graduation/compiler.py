"""Graduation compiler: a passing run's trace + checklist + test text → a CompiledScript, or the
reasons it is not safe to replay (always shown to the user).

Pure: no device, no model, no database. The rules it enforces:
  • locked checks come ONLY from values the test itself states (quoted text, prices,
    percentages, a switch's on/off), and only when the screen the agent cited showed them;
  • a verification clause with nothing checkable blocks graduation (no evidence-free PASS);
  • nothing secret is stored: a typed credential becomes a reference to the test text;
  • anything we cannot replay faithfully (invented input, one-time codes, position-only
    taps) blocks graduation with a plain-English reason.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from tamam.executors.core.redact import MARK, credentials_in
from tamam.executors.core.screen import screen_shows
from tamam.harness.clauses import (NEGATED, NEGATION, NOT_NAMES, NOT_SHOWN, QUOTED, is_verification,
                                   unquoted)
from tamam.graduation.script import (
    Check, ClauseCoverage, CompiledScript, Locator, Step, Target, TypedText,
)

MIN_TRACE_SCHEMA = 2

_AMOUNT = re.compile(r"[$€£]\s?\d+(?:[.,]\d+)?|\b\d+(?:\.\d+)?%")
# A claim about a control's STATE: only a selected / switch check may cover it.
_STATE = re.compile(r"\b(selected|highlighted|chosen|checked|enabled|disabled|applied|"
                    r"(?:is|are|shows? as|appears? as|remains?|stays?|turned|switched)\s+"
                    r"(?:on|off|active))\b", re.IGNORECASE)
_SELECTED_WORDS = re.compile(r"\b(selected|highlighted|chosen|checked|active|applied)\b",
                             re.IGNORECASE)
_STILL = re.compile(r"\b(still|remains?|stays?|persists?)\b", re.IGNORECASE)
# Names the test gives: Capitalised words, joined by short connectors ("Terms of Service").
_NAME = re.compile(r"\b([A-Z][\w'&-]*(?:\s+(?:of|and|&|the|for|to|in)?\s*[A-Z0-9][\w'&-]*)*)")
_LEADING_VERB = re.compile(r"^\s*(verify|confirm|check|ensure|open|tap|select|choose|go to|"
                           r"then|and|scroll to|find)\s+(that\s+|the\s+)?", re.IGNORECASE)
_CREDENTIAL_WORD = re.compile(r"\b(password|passcode|passwd|pwd|pin|otp|token|secret|api key)\b"
                              r"\s*(?:is|:|=)?\s*[\"'“]?([^\s\"'”]{2,})", re.IGNORECASE)
_NOT_SECRET = {"field", "fields", "box", "screen", "pad", "page", "prompt", "entry", "input",
               "and", "or", "the", "to", "for", "is", "reset"}
_PRONOUNS = {"its", "it", "their", "this", "that", "they", "them", "is", "are", "the"}
_ONE_TIME = re.compile(r"\b(otp|one[- ]time (?:code|password)|verification code|2fa|captcha)\b",
                       re.IGNORECASE)


@dataclass
class Decision:
    ready: bool
    reasons: list[str] = field(default_factory=list)
    script: CompiledScript | None = None

    def as_dict(self) -> dict:
        out: dict = {"ready": self.ready, "reasons": self.reasons}
        if self.script is not None:
            out["summary"] = self.script.checked_summary
        return out


def instructions_hash(text: str) -> str:
    """Whitespace-insensitive, so re-wrapping a test does not retire its script."""
    return hashlib.sha256(" ".join((text or "").split()).encode()).hexdigest()[:16]


def compile_run(records: list[dict], clauses: list[dict], test_text: str, *,
                passed: bool, run_error: str | None = None,
                source_run_id: str | None = None, allow_risky: bool = False) -> Decision:
    reasons: list[str] = []
    if not passed:
        reasons.append("The run did not pass.")
    if run_error:
        reasons.append(f"The run had trouble: {run_error[:120]}")
    recs = sorted((r for r in records if isinstance(r, dict) and "step" in r),
                  key=lambda r: r["step"])
    if not recs or any((r.get("schema") or 0) < MIN_TRACE_SCHEMA for r in recs):
        return Decision(False, reasons + ["This run was recorded before fast mode existed; "
                                          "the next passing run can be used."])
    if any(r.get("error") for r in recs):
        reasons.append("The device had trouble during this run (a step did not complete).")
    if _ONE_TIME.search(test_text or ""):
        reasons.append("The test needs a one-time code, which can't be replayed.")

    steps, step_reasons = _steps(recs, test_text)
    reasons += step_reasons
    if not allow_risky:
        reasons += risky_step_reasons(steps)
    checks, coverage, clause_reasons = _checks(recs, clauses, test_text, steps)
    reasons += clause_reasons
    if not steps and not checks and not any(c.kind == "verification" for c in coverage):
        reasons.append("The run took no replayable steps and has nothing to check.")
    _scroll_to_checks(steps, checks)

    size = next((r["screen"].get("size_pt") for r in recs if r.get("screen")), None) or []
    script = CompiledScript(test_text_hash=instructions_hash(test_text), source_run_id=source_run_id,
                            device_class=list(size), steps=steps, checks=checks, coverage=coverage)
    return Decision(ready=not reasons, reasons=reasons, script=script)


# ── steps ────────────────────────────────────────────────────────────────────


# Taps that change real data or spend money. Replay repeats every recorded tap on every run,
# so a test that does one of these is not replayed unless the project or test allows it
# (a test environment where repeating it is harmless). Fail-closed: a false alarm only costs
# speed (the AI keeps running the test), never a wrong verdict.
_RISKY = re.compile(
    r"\b(delete|remove|erase|trash|deactivate|close (?:my |your )?account|cancel (?:my |your )?"
    r"subscription|unsubscribe|buy|purchase|pay|checkout|place (?:the )?order|submit (?:order|"
    r"payment|application)|confirm (?:order|purchase|payment)|send|transfer|sign out|log ?out"
    r"|reset|clear all|apply now|book now|subscribe)\b", re.IGNORECASE)
# Harmless uses of those words (passwordless login asks to "send a code").
# Answers that decline a first-run / marketing prompt. They change no data, and whether the
# prompt appears at all varies between fresh devices.
PROMPT_DECLINES = {"maybe later", "not now", "no thanks", "no, thanks", "later", "skip", "skip for now",
                   "close", "dismiss", "not interested", "remind me later"}
_SAFE = re.compile(r"\b(re-?send|send (?:me )?(?:a |the )?(?:new )?(?:code|link|otp|pin)|"
                   r"reset (?:filters?|search|password))\b", re.IGNORECASE)
# A generic "yes" on a dialog: what it confirms is in the dialog's text, not the button's.
_CONFIRM = re.compile(r"^\s*(ok|okay|yes|confirm|continue|done|allow|agree|i agree|proceed|"
                      r"go|sure|submit|return|send)\s*$", re.IGNORECASE)


def _words(identifier: str) -> str:
    """'trashButton' / 'cart.badge.plus' / 'log_out' → words a pattern can match."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", identifier or "")
    return re.sub(r"[._\-]+", " ", spaced)


def _risky(text: str) -> bool:
    return bool(text) and bool(_RISKY.search(text)) and not _SAFE.search(text)


def risky_step_reasons(steps: list[Step]) -> list[str]:
    """Plain-English reasons for every replayed tap that changes data or spends money: by its
    label, its accessibility identifier, or — for a generic "OK"/"Yes" and taps by position —
    by the screen it was tapped on."""
    out = []
    for s in steps:
        if s.do not in ("tap", "tap_point", "long_press", "double_tap"):
            continue
        label = s.target.label if s.target is not None else ""
        ids = [_words(loc.value) for loc in (s.target.locators if s.target else [])
               if loc.kind == "identifier"]
        hit = next((t for t in [label, *ids] if _risky(t)), None)
        if hit is None and (s.do == "tap_point" or _CONFIRM.search(label or "")):
            hit = next((t for t in s.context if _risky(t)), None)
            if hit is not None:
                hit = f'{label or "a spot"}" on a screen saying "{hit}'
        if hit is not None:
            out.append(f'Step {s.source_step} taps "{hit}", which changes data or spends money; '
                       "fast replay would repeat it every run. Allow risky actions for this test "
                       "if that is fine (e.g. a test environment).")
    return out


def _target(t: dict | None) -> Target | None:
    if not t:
        return None
    locs = []
    if t.get("identifier"):
        locs.append(Locator(kind="identifier", value=t["identifier"]))
    if t.get("label"):
        locs.append(Locator(kind="type_label", value=f'{t["type"]}|{t["label"]}'))
    if not locs:
        return None
    return Target(type=t["type"], label=t.get("label") or "", locators=locs,
                  frame_frac=t.get("frame_frac") or [], toggled=t.get("toggled"))


def _context(rec: dict | None, n: int = 40) -> list[str]:
    return list(((rec or {}).get("screen") or {}).get("texts") or [])[:n]


def _anchors(rec: dict | None, n: int = 3) -> list[str]:
    return list(((rec or {}).get("screen") or {}).get("anchors") or [])[:n]


def _no_effect(rec: dict, nxt: dict | None) -> bool:
    """The action changed nothing: the next screen read is identical (by content hash)."""
    if nxt is None:
        return False
    a = (rec.get("screen") or {}).get("tree_hash")
    return bool(a) and a == (nxt.get("screen") or {}).get("tree_hash")


def _steps(recs: list[dict], test_text: str) -> tuple[list[Step], list[str]]:
    """The replay steps for an AI run's records, and the reasons any step can't be replayed."""
    return _StepCompiler(test_text).compile(recs)


class _StepCompiler:
    """Turns recorded actions into replay steps: one handler per action (see _STEP_HANDLERS).
    Swipes are gathered and become one "scroll until" step aimed at whatever comes next; a step
    replay could not perform is never dropped silently — it becomes a reason not to graduate."""

    def __init__(self, test_text: str) -> None:
        self.creds = credential_values(test_text)
        self.out: list[Step] = []
        self.reasons: list[str] = []
        self.swipes: list[dict] = []
        self.tap_points = 0

    def compile(self, recs: list[dict]) -> tuple[list[Step], list[str]]:
        for i, rec in enumerate(recs):
            kind = (rec.get("action") or {}).get("action")
            if kind in (None, "done", "wait"):
                continue          # "wait": replay polls for each step's screen instead of sleeping
            handler = _STEP_HANDLERS.get(kind)
            if handler is None:
                # An action replay cannot perform (a drag, a new verb): never silently drop it —
                # the script would skip a step the test depends on.
                self.reasons.append(f"Step {rec['step']}: a '{kind}' step cannot be replayed yet.")
                continue
            handler(self, rec, rec.get("action") or {}, recs[i + 1] if i + 1 < len(recs) else None)
        self.flush_swipes(None)
        if self.tap_points > 1:
            self.reasons.append(f"{self.tap_points} steps tapped by screen position only.")
        for s in self.out:
            if s.do == "tap" and s.target is not None and s.target.label.strip().lower() in PROMPT_DECLINES:
                s.optional = True
        return self.out, self.reasons

    def add(self, **step) -> None:
        self.out.append(Step(id=f"s{len(self.out) + 1}", **step))

    def flush_swipes(self, next_target: Target | None) -> None:
        if not self.swipes:
            return
        first = self.swipes[0]
        n = len(self.swipes)
        self.add(do="scroll_until", direction=first["action"].get("direction") or "up",
                 scroll_on=_target(first.get("target")), target=next_target,
                 max=(2 * n + 3) if next_target else n,
                 say=(f'Scroll until "{next_target.label}" appears' if next_target
                      else f"Scroll {n} time(s)"),
                 pre=_anchors(first), source_step=first["step"])
        self.swipes.clear()

    def swipe(self, rec: dict, a: dict, nxt: dict | None) -> None:
        if not _no_effect(rec, nxt):
            self.swipes.append(rec)

    def tap(self, rec: dict, a: dict, nxt: dict | None) -> None:
        tgt = _target(rec.get("target"))
        if tgt is None:
            self.reasons.append(f"Step {rec['step']}: the tapped element has no stable name.")
            return
        if _no_effect(rec, nxt):
            return  # a tap that changed nothing is not part of the path
        self.flush_swipes(tgt)
        self.add(do="tap", target=tgt, say=f'Tap "{tgt.label}"', pre=_anchors(rec),
                 post=_anchors(nxt), source_step=rec["step"], context=_context(rec))

    def type_text(self, rec: dict, a: dict, nxt: dict | None) -> None:
        self.flush_swipes(None)
        text, why = _typed(rec, a, self.creds)
        if why:
            self.reasons.append(f"Step {rec['step']}: {why}")
            return
        say = ("Type your password (hidden)" if text.credential is not None
               else f'Type "{text.literal}"')
        self.add(do="type", target=_target(rec.get("target")), text=text, clear=bool(a.get("clear")),
                 say=say, pre=_anchors(rec), post=_anchors(nxt), source_step=rec["step"])

    def tap_point(self, rec: dict, a: dict, nxt: dict | None) -> None:
        self.flush_swipes(None)
        self.tap_points += 1
        self.add(do="tap_point", point=[a.get("x"), a.get("y")],
                 say="Tap a spot on the screen (by position)", pre=_anchors(rec),
                 post=_anchors(nxt), source_step=rec["step"], context=_context(rec))

    def back(self, rec: dict, a: dict, nxt: dict | None) -> None:
        self.flush_swipes(None)
        self.add(do="back", say="Go back", pre=_anchors(rec), post=_anchors(nxt),
                 source_step=rec["step"])

    def background_app(self, rec: dict, a: dict, nxt: dict | None) -> None:
        self.flush_swipes(None)
        self.add(do="background", seconds=a.get("seconds"), say="Leave the app and come back",
                 pre=_anchors(rec), post=_anchors(nxt), source_step=rec["step"])

    def scroll_to(self, rec: dict, a: dict, nxt: dict | None) -> None:
        if _no_effect(rec, nxt):
            return          # the text was already on screen: nothing to replay
        prev = self.out[-1] if self.out else None
        if prev is not None and prev.do == "scroll_until" and prev.until_text == a.get("text"):
            # The agent needed two scroll_to calls (the first stopped at its swipe cap): one
            # replay step finds it — a second would scroll on past it. The LATER call's
            # direction is the one that found it (the first may have gone the wrong way).
            prev.max = (prev.max or 20) + 15
            prev.direction = a.get("direction") or prev.direction
            return
        self.flush_swipes(None)
        self.add(do="scroll_until", direction=a.get("direction") or "up", until_text=a.get("text"),
                 scroll_on=_target(rec.get("target")), max=20,
                 say=f'Scroll until "{a.get("text")}" appears', pre=_anchors(rec),
                 source_step=rec["step"])

    def press(self, rec: dict, a: dict, nxt: dict | None) -> None:
        """long_press / double_tap."""
        kind = a.get("action")
        tgt = _target(rec.get("target"))
        if tgt is None:
            self.reasons.append(f"Step {rec['step']}: the {kind.replace('_', '-')} "
                                "target has no stable name.")
            return
        self.flush_swipes(tgt)
        word = "Long-press" if kind == "long_press" else "Double-tap"
        self.add(do=kind, target=tgt, seconds=a.get("seconds"), say=f'{word} "{tgt.label}"',
                 pre=_anchors(rec), post=_anchors(nxt), source_step=rec["step"],
                 context=_context(rec))

    def hide_keyboard(self, rec: dict, a: dict, nxt: dict | None) -> None:
        if _no_effect(rec, nxt):
            return          # no keyboard was up
        self.flush_swipes(None)
        self.add(do="hide_keyboard", say="Hide the keyboard", pre=_anchors(rec),
                 post=_anchors(nxt), source_step=rec["step"])


# Recorded action → how it becomes replay steps. A new replayable action is one entry here
# (and its handler); anything missing refuses graduation with a reason.
_STEP_HANDLERS = {
    "swipe": _StepCompiler.swipe, "tap": _StepCompiler.tap, "type": _StepCompiler.type_text,
    "tap_point": _StepCompiler.tap_point, "back": _StepCompiler.back,
    "background_app": _StepCompiler.background_app, "scroll_to": _StepCompiler.scroll_to,
    "long_press": _StepCompiler.press, "double_tap": _StepCompiler.press,
    "hide_keyboard": _StepCompiler.hide_keyboard,
}


def _credential_words(test_text: str) -> set[str]:
    """Every word written right after a credential word, whatever it looks like: a script must
    never store one as a literal, even when the field it went into was not marked secure."""
    return {m.group(2).rstrip(".,;:)") for m in _CREDENTIAL_WORD.finditer(test_text or "")
            if m.group(2).rstrip(".,;:)").lower() not in _NOT_SECRET}


def credential_values(test_text: str) -> list[str]:
    """The test's credentials in a stable order; a script refers to one by its index, and
    replay fills it in from the test text with this same function."""
    return sorted(credentials_in(test_text) | _credential_words(test_text))


def _typed(rec: dict, a: dict, creds: list[str]) -> tuple[TypedText | None, str | None]:
    meta = a.get("typed") or {}
    raw = a.get("text")
    if raw and raw in creds:
        return TypedText(credential=creds.index(raw)), None
    if meta.get("in_test_text") and not meta.get("secure") and raw and raw != MARK:
        return TypedText(literal=raw), None
    if meta.get("in_test_text"):
        same = [i for i, c in enumerate(creds) if len(c) == meta.get("chars")]
        if len(same) == 1:
            return TypedText(credential=same[0]), None
        return None, "can't tell which value from the test was typed."
    return None, "typed a value the test doesn't contain (it would differ next time)."


# ── checks ───────────────────────────────────────────────────────────────────


def _names(text: str) -> list[str]:
    """Names a clause gives (Capitalised phrases, "Terms of Service"), after dropping a leading
    instruction word so "Verify Help opens" still yields "Help"."""
    body = _LEADING_VERB.sub("", text or "", count=1)
    out = []
    for m in _NAME.finditer(body):
        v = m.group(1).strip()
        words = v.split()
        while words and words[0].lower() in NOT_NAMES:
            words = words[1:]
        v = " ".join(words)
        if len(v) >= 3 and v.lower() not in NOT_NAMES:
            out.append(v)
    return out


def _checks(recs: list[dict], clauses: list[dict], test_text: str,
            steps: list[Step]) -> tuple[list[Check], list[ClauseCoverage], list[str]]:
    by_step = {r["step"]: r for r in recs}
    checks: list[Check] = []
    coverage: list[ClauseCoverage] = []
    reasons: list[str] = []
    for c in clauses or []:
        text = c.get("text") or ""
        kind = "verification" if is_verification(text) else "action"
        if c.get("status") != "done":
            reasons.append(f'"{text}" was not completed in this run.')
        rec = by_step.get(c.get("step"))
        found: list[Check] = []
        complete = False
        if kind == "verification" and rec is not None and c.get("status") == "done":
            # The cited step is trusted only as far as the discrimination rule allows: a value
            # already on screen before the action that led there never counts. The checklist
            # accepts evidence from the cited step or a neighbour (an agent can cite the step
            # it acted on rather than the screen that showed the result), so the compiler also
            # looks one step LATER. Never earlier: the step before has no screen before it to
            # prove the value appeared, so the discrimination rule would pass vacuously.
            for k in (c.get("step"), c.get("step") + 1):
                cand = by_step.get(k)
                if cand is None:
                    continue
                found, complete = _clause_checks(c, cand, test_text, steps, start=len(checks),
                                                 recs=recs)
                if found:
                    rec = cand
                    break
        checks += found
        anchors = list(((rec or {}).get("screen") or {}).get("anchors") or [])[:3]
        after = next((s.id for s in reversed(steps) if rec and s.source_step < rec["step"]), "")
        coverage.append(ClauseCoverage(clause_id=c.get("id") or "", clause=text, kind=kind,
                                       # Code-checked only when EVERY claim in it is: a clause with
                                       # one claim checked and another dropped also gets the AI look.
                                       checked_by_code=bool(found) and complete,
                                       after=after, on_screen=anchors))
        # A verification code cannot check is replayed with one AI look on its screen (it can
        # only confirm, never fail a run); the run says how each check was made.
    return checks, coverage, reasons


def _clause_checks(c: dict, rec: dict, test_text: str, steps: list[Step], start: int,
                   recs: list[dict]) -> tuple[list[Check], bool]:
    """Locked checks for one verification clause, or [] (= refuse). Every check must be
    stated by the test, shown on the evidence screen, and DISCRIMINATING: it would not also
    hold on the screen before the action that led here. Returns (checks, every claim covered)."""
    cs = _ClauseScreen(c, rec, test_text, steps, start, recs)
    grammar = unquoted(cs.text)
    if _STATE.search(grammar):
        return _state_checks(cs)
    if NEGATION.search(grammar):
        return _negated_checks(cs)
    return _positive_checks(cs)


class _ClauseScreen:
    """One verification clause and its evidence: the screen it was proven on, the screen
    before it, and the checks gathered so far."""

    def __init__(self, c: dict, rec: dict, test_text: str, steps: list[Step], start: int,
                 recs: list[dict]) -> None:
        self.clause_id, self.text = c.get("id") or "", c.get("text") or ""
        self.test_text, self.low_test = test_text, (test_text or "").lower()
        self.rec, self.recs, self.start = rec, recs, start
        self.screen = rec.get("screen") or {}
        self.shown = list(self.screen.get("texts") or [])
        self.before = next((r for r in reversed(recs) if r["step"] < rec["step"]), None)
        self.shown_before = list(((self.before or {}).get("screen") or {}).get("texts") or [])
        self.led_here = ((self.before or {}).get("target") or {}).get("label") or ""
        self.after = next((s.id for s in reversed(steps) if s.source_step < rec["step"]), "")
        self.anchors = list(self.screen.get("anchors") or [])[:3]
        self.still = bool(_STILL.search(self.text))
        self.out: list[Check] = []

    def stated(self, value: str) -> bool:
        return value.lower() in self.low_test

    def add(self, kind: str, value: str, expected: bool | None = None,
            with_text: str | None = None) -> None:
        if not any(x.kind == kind and x.text == value for x in self.out):
            self.out.append(Check(id=f"k{self.start + len(self.out) + 1}", clause_id=self.clause_id,
                                  clause=self.text, after=self.after, kind=kind, text=value,
                                  expected=expected, with_text=with_text, on_screen=self.anchors))

    def discriminating(self, value: str, with_text: str | None = None) -> bool:
        if self.before is None or self.still:
            return True              # the first screen, or a claim that something PERSISTS
        if self.led_here and value.lower() == self.led_here.lower():
            return False             # the control just tapped is still there if nothing happened
        return not screen_shows(self.shown_before, value, with_text)


def _state_checks(cs: _ClauseScreen) -> tuple[list[Check], bool]:
    """STATE claims ("selected", "is on"): only a state check may cover them."""
    names = [n.lower() for n in _names(cs.text)]
    if _SELECTED_WORDS.search(cs.text):
        for label in cs.screen.get("selected") or []:
            if label.lower() in names and cs.stated(label):
                cs.add("selected", label, True)
    words = re.findall(r"[a-z]+", cs.text.lower())
    want = True if "on" in words else False if "off" in words else None
    toggles = cs.screen.get("toggles") or {}
    if want is not None:
        for label in _named_switches(cs, toggles):
            if cs.stated(label) and toggles.get(label) is want:
                cs.add("toggled", label, want)
    return cs.out, bool(cs.out)


def _named_switches(cs: _ClauseScreen, toggles: dict) -> list[str]:
    """The switches a state clause is about: named in it, or ("it", "the switch") the last
    switch the run tapped."""
    named = [lbl for lbl in toggles if lbl.lower() in cs.text.lower()]
    if named or not re.search(r"\b(it|the switch|the toggle)\b", cs.text, re.IGNORECASE) \
            or _names(cs.text):
        return named
    tapped = [r.get("target") or {} for r in cs.recs if r["step"] < cs.rec["step"]
              and (r.get("action") or {}).get("action") == "tap"]
    last = next((t.get("label") for t in reversed(tapped) if t.get("type") == "Switch"), None)
    return [last] if last in toggles else []


def _negated_checks(cs: _ClauseScreen) -> tuple[list[Check], bool]:
    """NEGATED claims ("not shown", "no longer"): only absence checks it names, each with a
    positive check on the same screen (an empty or broken screen shows nothing at all)."""
    if cs.screen.get("texts_truncated"):
        return [], False
    named = {m.group(1).strip() for m in NEGATED.finditer(cs.text)}
    named |= {m.group(1).strip() for m in NOT_SHOWN.finditer(cs.text)}
    named |= {m.group(1).strip() for m in QUOTED.finditer(cs.text)}
    for v in sorted(named):
        v = " ".join(w for w in v.split() if w.lower() not in NOT_NAMES)
        if len(v) >= 3 and cs.stated(v) and not screen_shows(cs.shown, v):
            cs.add("absent", v)
    for n in _names(cs.text):
        if n not in named and cs.stated(n) and screen_shows(cs.shown, n) and cs.discriminating(n):
            cs.add("visible", n)
    ok = any(x.kind == "visible" for x in cs.out) and any(x.kind == "absent" for x in cs.out)
    return (cs.out, True) if ok else ([], False)


def _positive_checks(cs: _ClauseScreen) -> tuple[list[Check], bool]:
    """POSITIVE claims: quoted text, amounts bound by the clause's own words, names. Every quoted
    value and every amount is a claim that MUST get its check for the clause to count as
    checked by code; names are supporting context."""
    quoted = [m.group(1).strip() for m in QUOTED.finditer(cs.text)]
    amounts = [m.group(0).strip() for m in _AMOUNT.finditer(cs.text)]
    for v in quoted:
        if cs.stated(v) and screen_shows(cs.shown, v) and cs.discriminating(v):
            cs.add("visible", v)
    for v in amounts:
        _amount_check(cs, v)
    for n in _names(cs.text):
        if cs.stated(n) and screen_shows(cs.shown, n) and cs.discriminating(n):
            cs.add("visible", n)
    covered = {x.text for x in cs.out}
    return cs.out, bool(cs.out) and all(v in covered for v in quoted + amounts)


def _amount_check(cs: _ClauseScreen, v: str) -> None:
    """An amount is checked on the row it belongs to, or not at all."""
    if not cs.stated(v):
        return
    holders = [t for t in cs.shown if screen_shows([t], v)]
    if not holders:
        return
    words = [w for w in re.findall(r"[A-Za-z][\w'-]{2,}", cs.text)
             if w.lower() not in NOT_NAMES and w.lower() not in _PRONOUNS]
    partner = next((w for w in words if screen_shows(holders, v, w)), None)
    if partner is None and any(screen_shows(cs.shown, w) for w in words):
        return                   # "the total is $25" but the screen's Total row shows another amount
    if partner is None:
        # "its price is $5": bind to the item the TEST names — the one named closest before
        # the amount — and only if exactly ONE element on screen holds both.
        partner = _row_partner(cs.test_text, v, holders)
    if partner is None and len(holders) > 1:
        return                   # the amount is in several places; the clause can't say which
    # An amount bound to its row ("Golden Ticket — $5") is specific even when that row was
    # already on screen: it can only hold if THAT row shows THAT amount.
    if (partner is not None and partner.lower() not in ("total", "price")) or cs.discriminating(v, partner):
        cs.add("visible", v, with_text=partner)


def _row_partner(test_text: str, amount: str, holders: list[str]) -> str | None:
    """The name a test ties an amount to: the nearest name mentioned before it ("find the
    Golden Ticket, and verify its price is $5"), accepted only when exactly one on-screen
    element shows both — a name that sits in many rows ("Catalog item 5 — $5") binds nothing."""
    text = test_text or ""
    at = text.find(amount)
    names = _names(text)
    if at >= 0:
        names = sorted(names, key=lambda n: (text.rfind(n, 0, at) < 0, at - text.rfind(n, 0, at)))
    for n in names:
        rows = [h for h in holders if screen_shows([h], amount, n)]
        if len(rows) == 1:
            return n
    return None


def _scroll_to_checks(steps: list[Step], checks: list[Check]) -> None:
    """A scroll that was not followed by a tap was looking for something to check: replay
    scrolls UNTIL that shows, instead of a fixed number of swipes."""
    for s in steps:
        if s.do != "scroll_until" or s.target is not None:
            continue
        goal = next((c for c in checks if c.after == s.id and c.kind == "visible"), None)
        if goal is not None:
            s.until_text, s.until_with = goal.text, goal.with_text
            s.max = 2 * (s.max or 1) + 3
            s.say = f'Scroll until "{goal.with_text or goal.text}" appears'
