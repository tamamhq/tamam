"""Harness contracts — the seams the core is composed from.

A HarnessRun receives these as injected collaborators; it never reaches out to
free functions. Every one has a fake in tests, so the core runs with zero DB,
zero network, zero framework.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from tamam.executors.base import ExecutionResult, StepEvent
from tamam.harness.status import Lifecycle

if TYPE_CHECKING:
    # Only for type-checkers; a runtime import would create a cycle
    # (spec → verdict → checks). The annotation below stays a string.
    from tamam.harness.spec import JudgeResult


# ── Clock — injected so temporal guardrails are testable without real sleeps ──

class Clock(Protocol):
    def monotonic(self) -> float: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()


# ── Guardrails — structural limits as pure policies (no I/O, no async) ──

@dataclass
class RunState:
    """Mutable snapshot the run keeps current; guardrails read it, never write."""

    started_at: float
    last_event_at: float
    steps_seen: int
    now: float
    # Consecutive steps whose screen was identical to the prior step — i.e. the last
    # action had no effect. Lets a guardrail catch a spinning agent (e.g. retyping a
    # field that never accepts input). Updated by the run from each event's signature.
    no_progress_steps: int = 0
    last_signature: str | None = None
    # Consecutive steps repeating the SAME action (same target), regardless of whether
    # the screen churns — catches an agent fixating on one control (e.g. re-tapping a
    # filter icon) that the signature-based NoProgress misses when a highlight flickers.
    repeat_count: int = 0
    last_fingerprint: str | None = None
    # Total times each action fingerprint has been seen (not just consecutive) — catches
    # OSCILLATION (tap X → dismiss overlay → tap X → …), where consecutive counting resets.
    action_counts: dict[str, int] = field(default_factory=dict)

    @property
    def elapsed_s(self) -> float:
        return self.now - self.started_at

    @property
    def idle_s(self) -> float:
        return self.now - self.last_event_at


@runtime_checkable
class Guardrail(Protocol):
    def evaluate(self, state: RunState) -> str | None:
        """Return a breach reason (bare, no prefix), or None if within limits."""


# ── EventSink — where step + lifecycle events go (DB+bus in prod, list in tests) ──

class EventSink(Protocol):
    async def step(self, event: StepEvent) -> None: ...
    async def lifecycle(self, phase: Lifecycle, message: str) -> None: ...


# ── Judge — the LLM verifier, injected into the Verifier ──
# Returns a JudgeResult (tamam.harness.spec); kept a string annotation to avoid an
# import cycle (spec → verdict → checks, none of which import contracts).

class Judge(Protocol):
    async def evaluate(self, instructions: str, result: ExecutionResult) -> "JudgeResult": ...
