"""The clause plan — the harness, not the model, owns what "done" means.

Before the loop, the objective is split into ordered, independently checkable CLAUSES.
Every turn the agent sees the checklist, and reports progress on it (clause updates that
cite the step where the evidence was observed). A stop gate refuses a PASS while any
clause still lacks evidence, so "stopped before covering every clause" (the dominant
real-app failure) is caught in code rather than hoped away with prompt text.

Borrowed from coding harnesses, made stricter: Codex `update_plan` / Claude Code
TodoWrite keep a model-written plan but enforce it only in the prompt; mobile-use marks
subgoals complete only on observed evidence. Here the plan is SEEDED from the test
(the model cannot quietly shrink it) and completion is CHECKED at the stop boundary.
"""

from __future__ import annotations

import logging
import re

from tamam.executors.core.actions import ClauseUpdate, Done
from tamam.executors.ios.hooks import Hook, StepContext
from tamam.harness.clauses import Clause, ClausePlanner, is_verification, negated_names

logger = logging.getLogger(__name__)

# Navigation bar / tab bar bands (centre-y fraction): what NAMES a screen, not what it shows.
_TOP_BAR, _BOTTOM_BAR = 0.13, 0.87
_QUOTED = re.compile(r"[\"'“‘]([^\"'”’]{4,160})[\"'”’]")
GATE_REFUSALS = 2  # bounded, like a stop hook's `stop_hook_active` — never block forever


# ── the ledger hook ─────────────────────────────────────────────────────────


class ClauseLedger(Hook):
    """Seeds the checklist, shows it every turn, records the agent's cited updates, and
    gates `done`. Active only once the decider speaks in structured turns (a bare-action
    decider has no way to report progress, so gating it would only block)."""

    name = "clause_ledger"

    def __init__(self, planner: ClausePlanner, max_steps: int = 0,
                 preset: list[Clause] | None = None) -> None:
        self._planner = planner
        self._max_steps = max_steps
        self.clauses: list[Clause] = list(preset or [])
        # A preset checklist (hand-off from a fast replay) is already planned and already
        # gates `done`: the clauses replay proved are done, the rest must be evidenced here.
        self.active = bool(preset)
        self._planned = bool(preset)
        self._budget_warned = False
        self._seen: dict[int, list[str]] = {}   # step → content texts the screen showed
        self._shots: dict[int, str] = {}         # step → its raw screenshot (OCR fallback)
        self._tapped: set[str] = set()          # labels the agent tapped (titles mirror them)
        # step → {ref: (label, on/off)} for two-state controls, read from the app itself
        self._states: dict[int, dict[str, tuple[str, bool]]] = {}

    # plan once, lazily, so provisioning failures never pay for it
    async def before_observe(self, ctx: StepContext) -> None:
        if self._planned:
            return
        self._planned = True
        texts = await self._planner.plan(ctx.objective)
        self.clauses = [Clause(id=f"c{i}", text=t) for i, t in enumerate(texts, 1)]
        logger.info("clause plan: %s", [c.text for c in self.clauses])

    async def after_observe(self, ctx: StepContext) -> None:
        if ctx.space is not None:
            self._seen[ctx.step_num] = _content_texts(ctx.space)
            self._states[ctx.step_num] = {e.ref: (e.label or "", e.toggled) for e in ctx.space.elements
                                          if getattr(e, "toggled", None) is not None}
        if ctx.shot_path:
            self._shots[ctx.step_num] = ctx.shot_path
        if not self.clauses:
            return
        lines = ["Test checklist (the harness tracks it; every clause needs observed "
                 "evidence before you can pass):"]
        for c in self.clauses:
            mark = {"pending": "[ ]", "done": "[x]", "failed": "[!]"}[c.status]
            where = f"step {c.step}" if c.step else "fast replay"
            tail = f"  ({where}: {c.evidence})" if c.status != "pending" else ""
            lines.append(f"  {mark} {c.id}: {c.text}{tail}")
        failed = [c for c in self.clauses if c.status == "failed"]
        if failed:
            # Fail fast. One failed clause fails the test; every further step on a slow
            # app is time spent for nothing (the agent once took 10x the steps by going past a
            # failure).
            lines.append(f"STOP: {failed[0].id} has FAILED, so the test has failed. "
                         "Conclude now with done passed=false; do not test the rest.")
        pending = self.pending()
        if not failed and self._max_steps and not self._budget_warned and pending \
                and ctx.step_num >= int(self._max_steps * 0.75):
            self._budget_warned = True
            left = self._max_steps - ctx.step_num + 1
            lines.append(f"BUDGET: {left} steps left and {len(pending)} clause(s) still "
                         "open — go straight for them; if one cannot be done, mark it "
                         "failed and conclude honestly.")
        ctx.space.context = "\n".join(lines) + (
            f"\n\nYour notes so far: {ctx.memory}" if ctx.memory else "")

    def record(self, updates: list[ClauseUpdate], step_num: int) -> list[str]:
        """Apply the agent's updates. Evidence must cite a step that exists; a bad
        citation is rejected and the reason returned to the agent."""
        self.active = True
        problems = []
        by_id = {c.id: c for c in self.clauses}
        for u in updates:
            c = by_id.get(u.id)
            if c is None:
                problems.append(f"{u.id} is not a clause on the checklist")
                continue
            if not 1 <= u.step <= step_num:
                problems.append(f"{u.id} cites step {u.step}, which has not happened "
                                f"(current step is {step_num})")
                continue
            c.status, c.step, c.evidence = u.status, u.step, u.evidence.strip()[:200]
        return problems

    async def after_action(self, ctx: StepContext, action, note: str) -> str:  # noqa: ANN001
        ref = getattr(action, "ref", None)
        el = ctx.space.by_ref(ref) if (ref and ctx.space is not None) else None
        if el is not None and el.label and el.toggled is None:
            self._tapped.add(_norm(el.label))
        return note

    def unsupported(self) -> list[Clause]:
        """Done verification clauses whose evidence quotes nothing the screen itself showed
        at the cited step (±1) — only its title, navigation, or words not on screen at all.
        A blank page "opened with its title" is not a page that shows its content."""
        out = []
        for c in self.clauses:
            if c.status != "done" or c.step is None or not is_verification(c.text):
                continue
            seen = [t for k in (c.step - 1, c.step, c.step + 1) for t in self._seen.get(k, [])]
            # "X is not switched on" is a STATE claim about X (checked by contradicted()), not a
            # claim that X is absent from the screen — X is right there.
            absent = negated_names(c.text) if _claimed_state(c.text) is None else []
            if absent:
                # An ABSENCE claim ("the Wireless Mouse is not shown"): its evidence names what
                # is missing, which by definition is not on screen. Check the absence itself on
                # the cited screen instead — and that the screen shows something (a blank or
                # broken screen shows nothing, which proves no absence).
                here = self._seen.get(c.step, [])
                if here and not any(_norm(n) in t for n in absent for t in here):
                    continue
                out.append(c)
                continue
            content = [t for t in seen if t not in self._tapped]
            if not _cites(c.evidence, content) and not self._cites_in_pixels(c):
                out.append(c)
        return out

    def contradicted(self) -> list[tuple[Clause, str]]:
        """Done clauses that claim a control is ON or OFF while the app's own state for that
        control (the one the evidence cites by ref, or names) says the opposite. The screenshot
        never outranks the app's state" in the tree; the agent
        called it "on" from the picture and passed a negative test — a false pass)."""
        out = []
        for c in self.clauses:
            if c.status != "done" or c.step is None:
                continue
            claim = _claimed_state(c.text)
            if claim is None:
                continue
            states = self._states.get(c.step, {})
            cited = [states[r] for r in _REF.findall(c.evidence or "") if r in states]
            text = _norm(c.text)
            named = [v for v in states.values() if v[0] and _norm(v[0]) in text]
            wrong = next((v for v in cited + named if v[1] != claim), None)
            if wrong is not None:
                out.append((c, f'"{wrong[0]}" reads ({"on" if wrong[1] else "off"}) in the app at step {c.step}'))
        return out

    def _cites_in_pixels(self, c: Clause) -> bool:
        """The quote isn't in the accessibility tree — is it in the cited step's SCREENSHOT, as
        read by OCR (never the model's own reading)? A system sheet drawn outside the app
        (Apple Pay: 'Processing Payment') is real evidence the tree cannot show."""
        from tamam.executors.ios.grounder import ocr_lines
        for k in (c.step, c.step + 1, c.step - 1):
            lines = [_norm(t) for t in ocr_lines(self._shots.get(k))]
            if _cites(c.evidence, [t for t in lines if t not in self._tapped]):
                return True
        return False

    def pending(self) -> list[Clause]:
        return [c for c in self.clauses if c.status == "pending"]

    async def on_done(self, ctx: StepContext, done: Done) -> str | None:
        if not self.active or not done.passed:
            return None
        if ctx.refusals[self.name] >= GATE_REFUSALS:
            # The run must end (a gate never blocks forever) — but a PASS the gate kept
            # refusing is not a pass: whatever still lacks evidence is marked, and the verdict
            # becomes inconclusive (verdict.resolve_without_judge), never PASSED.
            for c in self.unsupported() + [c for c, _ in self.contradicted()]:
                c.unsupported = True
            return None
        failed = [c for c in self.clauses if c.status == "failed"]
        if failed:
            ids = ", ".join(f"{c.id} ({c.text})" for c in failed)
            return (f"[guardrail] Not accepted: you reported a PASS, but you marked {ids} "
                    "as FAILED. A test passes only if every clause holds — conclude with "
                    "passed=false, or re-check that clause and update it.")
        pending = self.pending()
        if pending:
            ids = "; ".join(f"{c.id}: {c.text}" for c in pending)
            return (f"[guardrail] Not accepted: these clauses have no observed evidence "
                    f"yet — {ids}. Carry each one out and cite the step where you saw the "
                    "result. If one genuinely cannot be done, mark it failed and conclude "
                    "with passed=false; a partial result reported honestly beats an "
                    "overclaimed pass.")
        clash = self.contradicted()
        if clash:
            ids = "; ".join(f"{c.id} ({why})" for c, why in clash)
            return (f"[guardrail] Not accepted: {ids}. A control's on/off state comes from the app, "
                    "never from how it looks in the screenshot. If the test needs it the other way, "
                    "that clause FAILED — mark it failed and conclude with passed=false.")
        weak = self.unsupported()
        if weak:
            ids = "; ".join(f"{c.id} (step {c.step}: {c.evidence!r})" for c in weak)
            return (f"[guardrail] Not accepted: the evidence for {ids} does not quote anything "
                    "the page itself showed — only its title or navigation, or words that were "
                    "not on screen. A page that opened is not a page that shows what the test "
                    "asks. Quote the specific on-screen text that proves each claim; if the page "
                    "does not show it (blank, still loading, an error, the wrong page), mark the "
                    "clause failed and conclude with passed=false.")
        return None


def _norm(text: str) -> str:
    t = (text or "").lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return " ".join(t.split())


def _content_texts(space) -> list[str]:  # noqa: ANN001
    """What a screen SHOWS: element labels outside the navigation and tab bars. A switch's
    label stays citable even when tapped — its state is read from the app, not claimed."""
    h = getattr(space, "screen_h", 0) or 0
    out = []
    for e in getattr(space, "elements", []):
        label = _norm(e.label or "")
        if not label:
            continue
        f = getattr(e, "frame", None)
        if h and f is not None and not _TOP_BAR <= (f.y + f.height / 2) / h <= _BOTTOM_BAR:
            continue
        out.append(label)
    return out


_REF = re.compile(r"\b(e\d+)\b")
_ON = re.compile(r"\b(on|enabled|checked|ticked|selected|active)\b", re.IGNORECASE)
_OFF = re.compile(r"\b(off|disabled|unchecked|unticked|unselected|inactive)\b", re.IGNORECASE)
_NOT = re.compile(r"\b(not|isn't|is not|n't)\b", re.IGNORECASE)


def _claimed_state(text: str) -> bool | None:
    """True / False when a clause claims a two-state control is on / off, else None."""
    t = text or ""
    if not re.search(r"\b(switch|switched|toggle|toggled|checkbox|checked|enabled|disabled|turned)\b",
                     t, re.IGNORECASE):
        return None
    on, off = bool(_ON.search(t)), bool(_OFF.search(t))
    if on == off:
        return None
    claim = on
    return (not claim) if _NOT.search(t) else claim


def _cites(evidence: str, content: list[str]) -> bool:
    """The evidence names a content text (whole, ≥4 chars), or quotes part of one."""
    ev = _norm(evidence)
    if any(len(t) >= 4 and t in ev for t in content):
        return True
    return any(q.strip() and any(_norm(q) in t for t in content) for q in _QUOTED.findall(evidence or "")
               if len(q.strip()) >= 4)
