"""Graduation eval: does a passing AI run compile into a sensible fast-replay script?

For each Shoply scenario on the CORRECT build the agent runs the test, then the compiler
turns the run into a script. "Sensible" = the locked checks are exactly what the test itself
states (at least the expected ones, never a value the test does not contain), or — when the
test states nothing code can check — no code check is locked (an AI look covers it). Kill criterion (plan
P1): fewer than 10 of 15 scenarios compile sensibly.

LIMIT: this scores how the compiled checks LOOK. Whether they catch bugs is proven only by
replaying each script against the buggy build — that is P2's mutation eval.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class ANY_OF(tuple):
    """Several acceptable expectations (each a list of checks, or None = refused)."""

    def __new__(cls, *options):
        return super().__new__(cls, options)


# did → the locked checks the compiled script must contain, as (kind, text[, expected]).
# None = the test states nothing code can check: the script must lock NO code check (each
# such clause is replayed with one AI look on its screen instead).
EXPECTED: dict[str, list[tuple] | None] = {
    "branding": [("visible", "Shoply")],
    "offer": [("visible", "20%")],          # "advertises 20% off": the stated value is 20%
    "list-price": [("visible", "$80")],
    "add-to-cart": [("visible", "Added to cart")],
    # "only the Wireless Mouse is listed": the Mouse was listed before the search too, so seeing
    # it proves nothing; "only" is not a stated value. Refusing is right.
    "search": None,
    # "only book products are shown, not the Wireless Mouse": an absence alone would pass an
    # empty list, and "book products" is not a stated name. Refusing is right.
    "filter": None,
    "menu-orders": [("visible", "No orders yet")],
    "cart-total": [("visible", "$35")],
    "offscreen-category": None,             # "only garden products are listed": no stated value
    "icon-only-collection": None,
    # "the Books filter is still applied" is a STATE claim: only the app's own selected state
    # may cover it (the chip's label is always on screen, so a visible check would mask #12).
    "resume-filter": ANY_OF([("selected", "Books", True)], None),
    "slow-reviews": None,
    "coverage-late-filter": None,
    "coverage-late-orders": [("visible", "No orders yet")],
    "infinite-catalog": [("visible", "$5")],
    "error-alert": None,
    "confirm-cancel": None,
    "toggle-switch": [("toggled", "Dark mode", True)],
    "checkout": [("visible", "Order Confirmed")],
    # Look-only defects: the page title matches either way, so no code check
    # discriminates — every clause must go to the AI look.
    "blank-terms": None,
    "half-loaded-help": None,
    "wrong-page": None,
    "error-banner": None,
    # A position-only tap compiles to a tap_point step; the stated thanks text is lockable.
    "rate-stars": ANY_OF([("visible", "Thanks for 4 stars")], None),
    "keyboard-form": ANY_OF([("visible", "Address saved")], None),
    "hold-redeem": ANY_OF([("visible", "Gift card redeemed")], None),
    "swipe-coupon": ANY_OF([("visible", "SAVE20")], None),
    "feedback": ANY_OF([("visible", "Thanks for your feedback")], None),
    "newsletter": ANY_OF([("visible", "Subscribed: ada@example.com")], None),
}


@dataclass
class GradResult:
    did: str
    run_passed: bool
    ready: bool
    reasons: list[str] = field(default_factory=list)
    checks: list[tuple] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    sensible: bool = False
    why: str = ""


def judge(did: str, run_passed: bool, decision, objective: str) -> GradResult:  # noqa: ANN001
    script = decision.script
    checks = [(c.kind, c.text) + ((c.expected,) if c.kind in ("toggled", "selected") else ())
              for c in (script.checks if script else [])]
    r = GradResult(did=did, run_passed=run_passed, ready=decision.ready,
                   reasons=decision.reasons, checks=checks,
                   steps=[s.say for s in (script.steps if script else [])])
    if not run_passed:
        r.why = "the AI run itself did not pass (not a compiler result)"
        return r
    want = EXPECTED.get(did, "unknown")
    stray = [c for c in checks if c[1].lower() not in objective.lower()]
    if stray:
        r.why = f"locked a value the test does not state: {stray}"
    elif want == "unknown":
        r.why = "no expectation recorded"
    else:
        options = list(want) if isinstance(want, ANY_OF) else [want]
        for opt in options:
            if opt is None:
                # "Refuse" is acceptable here: a refused compile is sensible whatever checks it
                # drafted on the way (they are never stored or replayed).
                ok, why = (not checks or not decision.ready,
                           f"locked a code check the test does not state: {checks}")
            else:
                missing = [w for w in opt if w not in checks]
                ok = decision.ready and not missing
                why = f"missing checks {missing}" if missing else f"refused: {decision.reasons}"
            if ok:
                r.sensible, r.why = True, ""
                break
            r.why = why
    return r


# Scenarios whose run taps something that changes data or spends money ("Pay", "Sign out").
RISKY = ("checkout", "confirm-cancel", "feedback", "newsletter")   # Send / Subscribe


def score_run(did: str, run_passed: bool, recs: list, clauses: list, objective: str,
              run_error: str | None = None):  # noqa: ANN201
    """Compile one AI run and score it: (decision compiled with risky actions allowed — a
    fixture — and its GradResult). Also checks the block path: without the opt-in a
    risky run must be refused and every other run must still graduate."""
    from tamam.graduation.compiler import compile_run
    decision = compile_run(recs, clauses, objective, passed=run_passed, run_error=run_error,
                           allow_risky=True)
    r = judge(did, run_passed, decision, objective)
    if decision.ready:
        strict = compile_run(recs, clauses, objective, passed=run_passed, run_error=run_error)
        if strict.ready == (did in RISKY):
            r.sensible, r.why = False, f"risk block wrong: ready={strict.ready} without opt-in"
    return decision, r
