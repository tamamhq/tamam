"""Eval data model — cases, per-run outcomes, and the scored report.

An EvalCase is the QA analog of a benchmark task: a target + an objective + the
KNOWN expected verdict (pass/fail), plus an optional independent oracle so ground
truth doesn't rely on the agent it is grading. Platform-agnostic: `target` carries
whatever the executor needs (iOS today, Android/web later).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Platform = Literal["ios", "android", "web"]
Expected = Literal["pass", "fail"]
Outcome = Literal["passed", "failed", "error"]
Difficulty = Literal["easy", "medium", "hard"]


class Target(BaseModel):
    """What/where to run — mirrors ExecutionSpec's target fields."""

    platform: Platform = "ios"
    executor_id: str = "ios_agent"
    app_build_ref: str | None = None   # path to the .app (fixture: correct|buggy build)
    app_bundle_id: str | None = None
    device: str | None = None
    start_url: str = ""                 # web, later


class EvalCase(BaseModel):
    id: str
    category: str                       # happy-path | planted-bug | multi-step | form | ...
    difficulty: Difficulty = "medium"
    objective: str                      # natural-language test the agent performs
    expected: Expected                  # the verdict a correct QA agent MUST return
    target: Target
    max_steps: int = 20
    # Independent oracle (optional): deterministic ground-truth on the captured final
    # accessibility text — confirms `expected` is actually true this run (fixture drift
    # guard), separate from the agent/judge that produced the verdict.
    require_text: list[str] = Field(default_factory=list)
    forbid_text: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class RunOutcome(BaseModel):
    """One execution of a case."""

    outcome: Outcome
    correct: bool                       # outcome matched case.expected
    disputed: bool = False
    self_pass: bool | None = None
    judge_pass: bool | None = None
    oracle_ok: bool | None = None       # independent oracle agreed with `expected`
    steps: int = 0
    duration_s: float | None = None
    tokens: int | None = None
    reason: str = ""
    error: str | None = None


class CaseResult(BaseModel):
    case: EvalCase
    runs: list[RunOutcome] = Field(default_factory=list)
