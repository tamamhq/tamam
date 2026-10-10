"""The compiled test: a deterministic replay of a passing AI run, plus locked checks.

Versioned JSON (pydantic), stored per test. Platform-neutral where it costs nothing:
targets carry a ranked list of locators, resolved by a per-platform resolver. Nothing
secret is ever stored: a typed credential is a reference to the test text, not a value.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Versioning rule: adding an OPTIONAL field (with a default) keeps the schema number; changing
# the meaning of a field bumps it. Replay must refuse a script with a newer schema than it knows.
SCRIPT_SCHEMA = 1
COMPILER_VERSION = "1.0"


class Locator(BaseModel):
    kind: Literal["identifier", "type_label"]
    value: str


class Target(BaseModel):
    """How to find an element again: by what survives the run, best locator first."""
    type: str
    label: str
    locators: list[Locator]
    frame_frac: list[float] = Field(default_factory=list)   # tie-breaker only
    toggled: bool | None = None


class TypedText(BaseModel):
    """What a type step enters. A literal only when the test spells it out and it is not a
    secret; otherwise the Nth credential written in the test, filled in at replay time."""
    literal: str | None = None
    credential: int | None = None   # index into credentials_in(test text)


class Step(BaseModel):
    id: str
    say: str                                   # plain English, for the user
    do: Literal["tap", "type", "scroll_until", "back", "wait", "background", "tap_point",
                "hide_keyboard", "long_press", "double_tap"]
    target: Target | None = None
    text: TypedText | None = None
    clear: bool = False
    direction: str | None = None               # scroll_until: the swipe direction
    max: int | None = None                     # scroll_until: give up after this many swipes
    until_text: str | None = None              # scroll_until with no target: until this shows
    until_with: str | None = None
    scroll_on: Target | None = None            # the element swiped on (a sideways row), else the screen
    point: list[float] | None = None           # tap_point: screen fractions
    seconds: float | None = None               # wait / background
    pre: list[str] = Field(default_factory=list)    # texts that must be on screen first
    context: list[str] = Field(default_factory=list)  # what the screen said (risk checks)
    # A tap that answered a first-run prompt ("Maybe Later"): fresh devices show such prompts
    # or not, run to run — skipped in replay when its prompt does not appear.
    optional: bool = False
    post: list[str] = Field(default_factory=list)   # texts expected after (classification)
    source_step: int                           # the AI run's step it came from


class Check(BaseModel):
    """A locked assertion: only values the test itself states, grounded on the screen the
    agent cited as evidence. Evaluated in code; never changed by a repair."""
    id: str
    clause_id: str
    clause: str
    after: str                                 # step id it is evaluated after ("" = at start)
    kind: Literal["visible", "absent", "toggled", "selected"]
    text: str                                  # the value (visible/absent) or the switch label
    with_text: str | None = None               # visible: must appear in the SAME element
    expected: bool | None = None               # toggled: on/off
    # The evidence screen's anchors: replay tells "check failed on the right screen" (an app
    # bug) from "we are on a different screen" (a miss → full AI run) by these.
    on_screen: list[str] = Field(default_factory=list)
    locked: Literal[True] = True


class ClauseCoverage(BaseModel):
    clause_id: str
    clause: str
    kind: Literal["action", "verification"]
    checked_by_code: bool
    after: str = ""                            # where it was evidenced (for an AI check later)
    on_screen: list[str] = Field(default_factory=list)


class CompiledScript(BaseModel):
    schema_: int = Field(default=SCRIPT_SCHEMA, alias="schema")
    compiler: str = COMPILER_VERSION
    test_text_hash: str
    source_run_id: str | None = None
    device_class: list[int] = Field(default_factory=list)   # screen size in points
    steps: list[Step]
    checks: list[Check]
    coverage: list[ClauseCoverage]
    # The app it was recorded on ({"build_id": ...}): a new build sends the script back to probation.
    app: dict = Field(default_factory=dict)

    @property
    def all_code_checked(self) -> bool:
        """Every verification clause is checked in code (no AI look needed): only such a
        script may run ON ITS OWN; others stay in probation, where a full AI run decides."""
        return all(c.checked_by_code for c in self.coverage if c.kind == "verification")

    model_config = {"populate_by_name": True}

    @property
    def checked_summary(self) -> str:
        v = [c for c in self.coverage if c.kind == "verification"]
        code = sum(c.checked_by_code for c in v)
        return f"{code} of {len(v)} checks by code, {len(v) - code} by an AI look on the screen"
