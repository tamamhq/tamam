"""The device agent's action vocabulary and per-step turn.

One action per step, chosen from what is on screen. Graduation compiles and replays these
same actions, so every platform's driver implements them; the brain's prompt is the only
place that explains them to the model.
"""

from __future__ import annotations

from typing import Literal, Union

from pydantic import BaseModel, Field, field_validator


class Tap(BaseModel):
    action: Literal["tap"] = "tap"
    ref: str
    reason: str = ""


class Type(BaseModel):
    action: Literal["type"] = "type"
    # ref is optional: an empty ref means "type into the field that is CURRENTLY FOCUSED"
    # — used after a `tap_point` focuses a rich-editor field that has no element ref.
    ref: str = ""
    text: str
    # Replace what the field already holds instead of appending to it.
    clear: bool = False
    reason: str = ""


class Wait(BaseModel):
    """Let the app finish something (a spinner, a load, an animation) before acting."""
    action: Literal["wait"] = "wait"
    seconds: float = 3.0
    reason: str = ""


class Back(BaseModel):
    """Go back one screen (the screen's Back control, else the system edge-swipe)."""
    action: Literal["back"] = "back"
    reason: str = ""


class Swipe(BaseModel):
    action: Literal["swipe"] = "swipe"
    direction: Literal["up", "down", "left", "right"] = "up"
    # Aim the swipe at one element's row. Without it the gesture is applied to the whole
    # window, which on a list-bodied screen scrolls (or opens) the list rather than the
    # strip you meant. ALWAYS set it when scrolling a row sideways.
    ref: str = ""
    reason: str = ""


class ScrollTo(BaseModel):
    """Scroll until an element containing `text` appears — the harness swipes (up to 15 times)
    without a decision per swipe. For an item far down a long list."""
    action: Literal["scroll_to"] = "scroll_to"
    text: str
    direction: Literal["up", "down", "left", "right"] = "up"
    ref: str = ""
    reason: str = ""


class Drag(BaseModel):
    """Press, hold and move an element to another element (or a screen point): drag-and-drop,
    reordering, a slider thumb. For flicking a card or a row aside, use swipe WITH its ref."""
    action: Literal["drag"] = "drag"
    ref: str
    to_ref: str = ""
    to_x: float | None = None
    to_y: float | None = None
    reason: str = ""


class LongPress(BaseModel):
    """Touch and hold an element: a context menu, a "hold to confirm" button."""
    action: Literal["long_press"] = "long_press"
    ref: str
    seconds: float = 1.0
    reason: str = ""


class DoubleTap(BaseModel):
    action: Literal["double_tap"] = "double_tap"
    ref: str
    reason: str = ""


class HideKeyboard(BaseModel):
    """Put the on-screen keyboard away (its Done/Dismiss key only — never return/Go/Send,
    which submit). Use it when what you need next is marked "under the keyboard"."""
    action: Literal["hide_keyboard"] = "hide_keyboard"
    reason: str = ""


class TapPoint(BaseModel):
    """Last-resort tap at a screen COORDINATE the agent sees in the screenshot, for targets
    that have NO element ref (e.g. text inside a rich/RN editor that the a11y tree doesn't
    expose). x,y are fractions of the screen: x=0 left→1 right, y=0 top→1 bottom."""
    action: Literal["tap_point"] = "tap_point"
    x: float
    y: float
    reason: str = ""


class BackgroundApp(BaseModel):
    """Send the app to the background, then bring it back to the foreground. Use it
    when the objective asks about backgrounding, resuming, or whether something
    survives leaving and re-entering the app."""
    action: Literal["background_app"] = "background_app"
    seconds: float = 2.0
    reason: str = ""


BLOCKED_CODES = ("missing_action", "cannot_reach", "system_ui", "test_setup")


class Done(BaseModel):
    action: Literal["done"] = "done"
    passed: bool
    reason: str  # <= 12 words, concrete
    reasoning: str = ""
    repro_steps: list[str] = Field(default_factory=list)
    # Set (with passed=false) when YOU could not perform a step — not when the app did the
    # wrong thing. Scored as inconclusive, never as an app failure. Lenient on purpose:
    # a value outside the list ("none", "") means not blocked — a schema reject would cost
    # the whole run (an output-retry error), which is worse than an unset code.
    blocked: str | None = Field(default=None, description="missing_action | cannot_reach | "
                                "system_ui | test_setup — only when YOU could not perform a step")

    @field_validator("blocked", mode="before")
    @classmethod
    def _known_code(cls, v: object) -> str | None:
        code = str(v or "").strip().lower().replace(" ", "_").replace("-", "_")
        return code if code in BLOCKED_CODES else None


NextAction = Union[Tap, Type, Swipe, ScrollTo, TapPoint, LongPress, DoubleTap, Drag, HideKeyboard,
                   BackgroundApp, Wait, Back, Done]


class ClauseUpdate(BaseModel):
    """The agent's report on one checklist clause, citing where it saw the evidence."""

    id: str
    status: Literal["done", "failed"]
    step: int = Field(description="the step number whose screen showed the evidence")
    evidence: str = Field(description="what was observed, <= 20 words")


class Turn(BaseModel):
    """One structured turn (browser-use's shape): judge the last action, carry notes
    forward, report checklist progress, then act. The evaluation forces the agent to
    notice a tap that did nothing; the memory survives the elided middle of a long
    history; the clause updates feed the harness's stop gate."""

    evaluation_previous: str = Field(
        default="", description="did your LAST action do what you intended? "
        "'success', 'failed' or 'unknown', plus why in <= 12 words, judged from the current screen")
    memory: str = Field(
        default="", description="notes to carry forward (<= 30 words): values you will "
        "need to compare later, what is next. Not the checklist, not element refs")
    clause_updates: list[ClauseUpdate] = Field(
        default_factory=list, description="checklist clauses whose outcome you have now "
        "OBSERVED, each citing the step number where you saw it")
    action: NextAction
