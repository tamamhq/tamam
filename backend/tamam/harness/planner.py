"""Autonomous verification planner — the harness decides what to verify in code.

Everything CAN be judged by the LLM, but exact facts (a specific label, price,
spelling, confirmation string) are more reliably checked deterministically than by
LLM judgment — LLMs gloss over one-character differences ("Shopply" vs "Shoply").

The planner reads a natural-language objective and emits deterministic checks for the
exact strings it names. The USER never picks these — the harness routes the exact
parts to code (tools) and leaves the semantic residue to the judge. This is
code-before-prompt made automatic.

OFF BY DEFAULT (TAMAM_AUTO_PLAN): the LLM over-extracts *concept* words from capability
objectives ("login form has SSO and email options" → text_present("SSO")) into literal
checks no real UI renders, false-FAILing correct runs. The intended deterministic layer
is test GRADUATION (a passing run compiled into a replay script with locked checks), not
guessed assertions — see docs/guide/test-graduation.md.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from tamam.harness.checks import Check


class PlannedCheck(BaseModel):
    kind: Literal["text_present", "text_absent"]
    value: str


class Plan(BaseModel):
    checks: list[PlannedCheck] = Field(default_factory=list)


SYSTEM = """\
You convert a QA test objective into DETERMINISTIC checks. A deterministic check
verifies an exact fact in code (no LLM) against the final screen text — this is more
reliable than judgment for specific strings, so extract every exact string the
objective pins down.

Emit:
- text_present  — for each exact string the objective says MUST appear (a brand name,
  a price like "$80", a label like "Order Confirmed", a specific value/spelling).
- text_absent   — for each exact string that must NOT appear.

Rules:
- Use the EXACT string from the objective (respect spelling, case, punctuation, "$").
- Only emit a check when the objective names a concrete literal string. Do NOT invent
  checks for vague, visual, positional, or subjective goals — leave those to the judge.
- Emitting zero checks is fine and correct when nothing exact is asserted.
"""


class AutoPlanner:
    """LLM planner → list[Check]. Cached per objective (evals repeat the same ones)."""

    def __init__(self, model: str) -> None:
        self._model = model
        self._agent = None
        self._cache: dict[str, list[Check]] = {}

    def _get_agent(self):
        from pydantic_ai import Agent

        if self._agent is None:
            self._agent = Agent(self._model, output_type=Plan, system_prompt=SYSTEM)
        return self._agent

    async def plan(self, objective: str) -> list[Check]:
        if objective in self._cache:
            return self._cache[objective]
        try:
            run = await self._get_agent().run(objective)
            checks = [Check(kind=c.kind, value=c.value) for c in run.output.checks]
        except Exception:
            checks = []  # planner is an enhancement — never fail the run
        self._cache[objective] = checks
        return checks
