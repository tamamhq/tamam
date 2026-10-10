"""Layer 1 of the verification pipeline: deterministic checks.

Zero LLM tokens. A rule can't hallucinate. Any verification that CAN be a rule
MUST be a rule; the judge only handles what rules cannot decide.

Adding a check kind = one @check("kind") function (plus widening CheckKind so
API payloads validate). Nothing else changes.
"""

from __future__ import annotations

import re
from typing import Callable, Literal

from pydantic import BaseModel, Field

from tamam.executors.base import ExecutionResult

CheckKind = Literal[
    "url_matches",
    "text_present",
    "text_absent",
    "element_exists",
    "no_console_errors",
    "no_failed_requests",
    "no_crashes",
    "action_used",
]


class Check(BaseModel):
    kind: CheckKind
    value: str | None = None  # pattern / text / selector, depending on kind


class CheckOutcome(BaseModel):
    check: Check
    passed: bool
    evidence: str  # what was observed — cited in the verdict reason


class ChecksResult(BaseModel):
    outcomes: list[CheckOutcome] = Field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(o.passed for o in self.outcomes)

    @property
    def failures(self) -> list[CheckOutcome]:
        return [o for o in self.outcomes if not o.passed]

    def failure_reason(self) -> str:
        parts = [f"{o.check.kind}({o.check.value or ''}): {o.evidence}" for o in self.failures]
        return "Deterministic check failed — " + "; ".join(parts)


# ── registry ──────────────────────────────────────────────────────────────

Evaluator = Callable[[str, ExecutionResult], tuple[bool, str]]  # → (passed, evidence)


class CheckDescriptor(BaseModel):
    """Everything a client needs to render a check picker — semantics stay here."""

    kind: str
    label: str
    description: str
    requires_value: bool = True
    placeholder: str | None = None


_EVALUATORS: dict[str, Evaluator] = {}
_DESCRIPTORS: dict[str, CheckDescriptor] = {}


def check(
    kind: str,
    *,
    label: str,
    description: str,
    requires_value: bool = True,
    placeholder: str | None = None,
) -> Callable[[Evaluator], Evaluator]:
    def decorator(fn: Evaluator) -> Evaluator:
        if kind in _EVALUATORS:
            raise ValueError(f"Check kind {kind!r} already registered")
        _EVALUATORS[kind] = fn
        _DESCRIPTORS[kind] = CheckDescriptor(
            kind=kind,
            label=label,
            description=description,
            requires_value=requires_value,
            placeholder=placeholder,
        )
        return fn

    return decorator


def registered_checks() -> list[str]:
    return sorted(_EVALUATORS)


def check_descriptors() -> list[CheckDescriptor]:
    return [_DESCRIPTORS[kind] for kind in sorted(_DESCRIPTORS)]


def run_checks(checks: list[Check], result: ExecutionResult) -> ChecksResult:
    outcomes = []
    for c in checks:
        evaluator = _EVALUATORS[c.kind]  # kinds pre-validated by the Check model
        passed, evidence = evaluator(c.value or "", result)
        outcomes.append(CheckOutcome(check=c, passed=passed, evidence=evidence))
    return ChecksResult(outcomes=outcomes)


# ── built-in checks ───────────────────────────────────────────────────────


@check(
    "url_matches",
    label="URL matches",
    description="Final URL matches a regex pattern",
    placeholder="/checkout/success",
)
def _url_matches(value: str, result: ExecutionResult) -> tuple[bool, str]:
    # No URL context (final_url is None, e.g. a native/mobile run) → a URL assertion is not
    # applicable; do NOT manufacture a false FAIL that would short-circuit the judge. Web
    # executors always set a string (even ""), so a genuine empty-URL web case still evaluates.
    if result.final_url is None:
        return True, "url_matches not applicable — run has no URL context (native/mobile)"
    return bool(re.search(value, result.final_url)), f"final URL was {result.final_url!r}"


@check(
    "text_present",
    label="Text present",
    description="Text appears in the final page (case-insensitive)",
    placeholder="order confirmed",
)
def _text_present(value: str, result: ExecutionResult) -> tuple[bool, str]:
    passed = value.lower() in (result.final_text or "").lower()
    return passed, f"text {value!r} {'found' if passed else 'not found'} in final page"


@check(
    "text_absent",
    label="Text absent",
    description="Text does NOT appear in the final page (case-insensitive)",
    placeholder="error",
)
def _text_absent(value: str, result: ExecutionResult) -> tuple[bool, str]:
    passed = value.lower() not in (result.final_text or "").lower()
    return passed, f"text {value!r} {'absent from' if passed else 'present in'} final page"


@check(
    "element_exists",
    label="Marker exists",
    description="A literal marker string appears in the final page",
    placeholder="data-testid=cart-count",
)
def _element_exists(value: str, result: ExecutionResult) -> tuple[bool, str]:
    # v1 evidence bundle carries DOM text, not a queryable tree; treat the
    # selector as a literal marker. Upgraded when executors ship DOM snapshots.
    passed = value in (result.final_text or "")
    return passed, f"marker {value!r} {'found' if passed else 'not found'} in final page"


# Some objectives can only be satisfied by performing a specific ACTION — no amount of
# looking at screens substitutes for it. Claiming such an objective without the action in
# the step log is a false pass, so the harness checks the log rather than trusting the
# claim. Keyed by capability, not by any app's wording.
ACTION_CUES: dict[str, tuple[str, ...]] = {
    # Phrases, not words: "background" alone also means a colour, which is exactly how a
    # keyword rule turns into a false failure.
    "background_app": ("to the background", "into the background", "in the background",
                       "background the app", "backgrounding", "foreground",
                       "reopen the app", "re-open the app", "relaunch the app",
                       "leave the app and", "minimise the app", "minimize the app"),
}


def required_actions(objective: str) -> set[str]:
    """Actions this objective cannot honestly be completed without."""
    low = objective.lower()
    return {a for a, cues in ACTION_CUES.items() if any(c in low for c in cues)}


@check(
    "action_used",
    label="Action was performed",
    description="The run actually carried out an action the objective requires",
    placeholder="background_app",
)
def _action_used(value: str, result: ExecutionResult) -> tuple[bool, str]:
    """Some objectives cannot be satisfied by looking — only by DOING. The step log
    says whether it was done, so the claim is checkable in code instead of taken on
    trust from either the agent or the judge."""
    used = {a.get("action") for s in result.steps for a in (s.actions or [])}
    passed = value in used
    return passed, (f"`{value}` was performed" if passed else
                    f"the objective requires `{value}`, which the run never performed")


@check(
    "no_console_errors",
    label="No console errors",
    description="No errors were logged (the browser console, or the app's device log)",
    requires_value=False,
)
def _no_console_errors(value: str, result: ExecutionResult) -> tuple[bool, str]:
    errors = result.diagnostics.errors
    if not errors:
        return True, "no errors logged"
    return False, f"{len(errors)} error(s) logged, first: {errors[0]!r}"


@check(
    "no_crashes",
    label="No crashes",
    description="The app did not crash",
    requires_value=False,
)
def _no_crashes(value: str, result: ExecutionResult) -> tuple[bool, str]:
    crashes = result.diagnostics.crashes
    if not crashes:
        return True, "no crashes"
    return False, f"{len(crashes)} crash(es), first: {crashes[0]!r}"


@check(
    "no_failed_requests",
    label="No failed requests",
    description="No network request failed (4xx/5xx); on an app, no crash either",
    requires_value=False,
)
def _no_failed_requests(value: str, result: ExecutionResult) -> tuple[bool, str]:
    # Saved app tests used this kind before crashes had their own check (no_crashes), so on
    # an app run it still counts crashes.
    failed = result.diagnostics.failed_requests + [{"crash": c} for c in result.diagnostics.crashes]
    if not failed:
        return True, "no failed requests"
    return False, f"{len(failed)} failed request(s), first: {failed[0]}"
