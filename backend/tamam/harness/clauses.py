"""Clause language: how a test's sentences are read, shared by every platform.

A test is split into clauses (one action or outcome each) that the agent must prove one by one.
This module holds the clause model, the planners that split a test, and the rules that classify
a clause: an ACTION to take, or a CLAIM to verify on screen (and which names a negated claim
says must be absent). The agent's ledger and graduation's compiler both read clauses with these
same rules, so a claim can't be "verified" by one and "an action" to the other.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel

logger = logging.getLogger(__name__)

MAX_CLAUSES = 8


@dataclass
class Clause:
    id: str
    text: str
    status: Literal["pending", "done", "failed"] = "pending"
    step: int | None = None   # step whose observation is the evidence
    evidence: str = ""
    how: str | None = None    # set when not the agent's own evidence ("code": proven by replay)
    # The agent called it done, but its evidence never quoted the screen and the gate ran out
    # of refusals: a PASS resting on it is not a pass (see verdict.resolve_without_judge).
    unsupported: bool = False

    def as_dict(self) -> dict:
        d = {"id": self.id, "text": self.text, "status": self.status,
             "step": self.step, "evidence": self.evidence}
        if self.how:
            d["how"] = self.how
        if self.unsupported:
            d["unsupported"] = True
        return d


# ── planners ────────────────────────────────────────────────────────────────


class ClausePlanner(Protocol):
    async def plan(self, objective: str) -> list[str]: ...


_SPLIT = re.compile(
    r"(?:\n+|;\s+|(?<=[.!?])\s+(?=[A-Z])|,?\s+\bthen\b\s+|,?\s+\band then\b\s+|"
    r"\s+\bafter that\b,?\s+|\s+\bfinally\b,?\s+)",
    re.IGNORECASE,
)
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")


def fit_clauses(parts: list[str]) -> list[str]:
    """At most MAX_CLAUSES, dropping NOTHING: the parts past the limit are folded into the last
    clause, so each one still needs evidence before the test can pass."""
    if len(parts) <= MAX_CLAUSES:
        return parts
    return parts[:MAX_CLAUSES - 1] + ["; then ".join(parts[MAX_CLAUSES - 1:])]


class HeuristicPlanner:
    """Deterministic split on sequencing words and sentence/line boundaries. The
    fallback when the model planner is unavailable, and the planner in hermetic tests.
    Deliberately conservative: it never invents clauses, only separates stated ones."""

    async def plan(self, objective: str) -> list[str]:
        parts = [_BULLET.sub("", p).strip(" .,;") for p in _SPLIT.split(objective or "")]
        parts = [p for p in parts if len(p.split()) >= 2]
        return fit_clauses(parts) or [objective.strip()]


PLANNER_PROMPT = """\
Split a mobile QA test objective into its ordered list of CLAUSES: the separate things
that must each be DONE and OBSERVED for the test to pass.

Rules:
- One clause = one user action or outcome that can be checked on screen
  ("Select the Toys category", "The results list shows only toys").
- Keep the objective's own wording; do not add steps it does not ask for, and do not
  drop any it does ("and", "then", "also", "after", "survives" usually introduce one).
- Preconditions and test data (credentials, account state) are NOT clauses.
- An expected result that is stated is its own clause.
- At most {max} clauses, in the order they must happen.
"""


class _PlanOut(BaseModel):
    clauses: list[str]


class ModelPlanner:
    """One small structured call per run; falls back to the heuristic on any failure."""

    def __init__(self, model: str, fallback: ClausePlanner | None = None) -> None:
        self._model = model
        self._fallback = fallback or HeuristicPlanner()
        self.total_tokens = 0

    async def plan(self, objective: str) -> list[str]:
        try:
            from pydantic_ai import Agent

            agent = Agent(self._model, output_type=_PlanOut,
                          system_prompt=PLANNER_PROMPT.format(max=MAX_CLAUSES))
            run = await agent.run(objective)
            self.total_tokens += getattr(run.usage, "total_tokens", 0) or 0
            got = fit_clauses([c.strip() for c in run.output.clauses if c.strip()])
            if got:
                return got
        except Exception:
            logger.warning("model clause planner failed; using heuristic split", exc_info=True)
        return await self._fallback.plan(objective)


# ── classifying a clause ────────────────────────────────────────────────────

# A clause is an ACTION only when it starts with an action verb; anything else is a claim about
# the app and must be checked — an unrecognised clause blocks graduation, never slips through.
ACTION_START = re.compile(
    r"^\s*(?:then\s+|and\s+|first,?\s+|next,?\s+)?(tap|open|go|navigate|select|choose|pick|"
    r"type|enter|fill|scroll|swipe|add|press|click|launch|sign in|log in|search|send|return|"
    r"turn|toggle|switch|change|edit|set|apply|save|close|dismiss|cancel|background|bring|"
    r"leave|come back|start|find|attempt|try|clear|delete|remove|upload|submit|create|"
    r"browse|use|wait|log out|sign out|read|note|look at|remember|"
    # Gestures are actions too ("Pull down to refresh", "Long-press …", "Hold …", "Drag …").
    r"pull|drag|hold|long[- ]press|double[- ]tap|flick|pinch|zoom|rotate|shake|refresh|"
    r"claim|accept|decline|allow|deny|confirm|complete|"
    # Only words that are unambiguously verbs: a noun here ("Order total is $5", "Filter shows
    # …") would turn a claim into an unchecked action. "Skip the introduction if it
    # appears" was treated as a claim, and a Wikipedia pass was refused for lacking evidence.
    r"skip|continue|agree|follow|share|copy|paste|pause|bookmark|subscribe|purchase|buy|pay|"
    r"download|install|reload|expand|collapse|unlock|sign up|register|reserve|invite|reply|"
    r"publish|archive|rename|duplicate|undo|retry|write|"
    # A prohibition is a constraint on the path, kept by replaying the recorded steps exactly.
    r"do not|don't|never|avoid)\b", re.IGNORECASE)
# A leading verb-word that NAMES a control ("Save button …", "Share icon …") describes that
# control: the clause is a claim about it, not an action to take.
NAMES_A_CONTROL = re.compile(
    r"^\s*\w+\s+(button|link|tab|key|icon|option|field|menu|toggle|switch|label|banner|screen|"
    r"page|sheet|dialog|popup|badge|chip)s?\s+(is|are|was|were|shows?|appears?|reads?|says|"
    r"displays?|stays?|remains?|becomes?|has|have|turns|changes|looks)\b", re.IGNORECASE)
VERIFY_WORDS = re.compile(r"\b(verify|ensure|make sure|pass if|fail if)\b", re.IGNORECASE)
VERIFY_START = re.compile(r"^\s*(?:then\s+|and\s+)?(confirm|check)\b", re.IGNORECASE)
QUOTED = re.compile(r"(?<!\w)['\"‘“]([^'\"’”]{2,80})['\"’”](?!\w)")

# Any negation: such a clause may only be covered by an ABSENCE check it names explicitly.
NEGATION = re.compile(r"\b(not|no|n't|no longer|without|never|disappears?|disappeared|removed|"
                       r"hidden|empty|gone|none)\b|n't\b", re.IGNORECASE)
NEGATED = re.compile(r"\b(?:not|no|without)\s+(?:the\s+|any\s+)?((?:[A-Z][\w'&-]*\s?){1,4})")
NOT_SHOWN = re.compile(r"((?:[A-Z][\w'&-]*\s?){1,4})\s+(?:is|are)\s*(?:not|n't)\s+"
                        r"(?:shown|listed|visible|displayed|present|there|on screen)\b")
NOT_NAMES = {"verify", "check", "confirm", "ensure", "open", "tap", "then", "the", "in", "on",
              "go", "and", "a", "an", "it", "if", "select", "scroll", "add", "type", "make",
              "pass", "fail", "choose"}

PLACE = re.compile(r"^\s*(?:in|on|from|at|inside|within)\s+[^,]{1,40},\s*", re.IGNORECASE)


def unquoted(text: str) -> str:
    """The clause with quoted values blanked: words inside quotes ('No orders yet') are the
    app's text, not the clause's grammar."""
    return QUOTED.sub('""', text or "")


def negated_names(text: str) -> set[str]:
    """What a negated claim says must NOT be there ("the Wireless Mouse is not shown",
    "no 'Couldn't save'"): the names after not/no/without, before "is not shown", or quoted."""
    if not NEGATION.search(unquoted(text)):
        return set()
    named = {m.group(1).strip() for m in NEGATED.finditer(text)}
    named |= {m.group(1).strip() for m in NOT_SHOWN.finditer(text)}
    named |= {m.group(1).strip() for m in QUOTED.finditer(text)}
    out = set()
    for v in named:
        v = " ".join(w for w in v.split() if w.lower() not in NOT_NAMES)
        if len(v) >= 3:
            out.add(v)
    return out


def is_verification(text: str) -> bool:
    """Default: a claim to check. Only a clause that STARTS with an action verb (after an
    optional place, "In the Shop, type …") and makes no explicit verify / pass-if claim is an
    action covered by the step reaching its screen."""
    bare = unquoted(text)
    if VERIFY_WORDS.search(bare) or VERIFY_START.search(PLACE.sub("", bare, count=1)):
        return True
    rest = PLACE.sub("", bare, count=1)
    if NAMES_A_CONTROL.search(rest):
        return True     # "Save button is greyed out", "Skip link is hidden": a claim about it
    return not ACTION_START.search(rest)
