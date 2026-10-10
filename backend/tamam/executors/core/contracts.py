"""Seams for the device agent — one protocol per layer, each with a fake in tests.

Mirrors the web plugin's contracts: DeviceProvider (which device) →
DeviceDriver (intent → actions + artifacts) → Perceiver (screen → ActionSpace).
Grounding is by coordinate: every element carries its on-screen frame, and the
driver taps the frame's center — no re-locating, no selector drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


# The element roles shared code may rely on. They are XCUITest's names, because iOS came
# first; every other perceiver maps its native classes onto them (an Android
# android.widget.Button or a web <button> is a "Button", an EditText or <input> a "TextField").
# Graduation keys recorded steps on (role, label), so a role must be stable for one control.
ROLES = frozenset({
    "Button", "Cell", "Link", "TextField", "SecureTextField", "SearchField", "TextView", "Switch",
    "Slider",
    "SegmentedControl", "MenuItem", "MenuButton", "TabBarButton", "PickerWheel", "StaticText",
    "Image", "Other",
})


class SessionLost(Exception):
    """The session died during an action and the reconnect budget is spent."""


@dataclass(frozen=True)
class Frame:
    x: float
    y: float
    width: float
    height: float

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.width / 2, self.y + self.height / 2)


@dataclass(frozen=True)
class Element:
    """One actionable thing on the screen. `ref` is the label the brain names
    ("tap e5"); grounding uses the frame's center, so any driver can hit it."""

    ref: str
    type: str            # a role from ROLES ("Button", "TextField", "Cell", …)
    label: str           # accessible name / label / value shown to the brain
    frame: Frame
    enabled: bool = True
    # Text-input state, so the brain can tell an empty field from a filled one (an
    # empty field otherwise looks identical to a full one — the cause of silent
    # form-submit failures). `value` is shown for normal fields; for SecureTextField
    # it is ALWAYS "" and only `filled` is exposed — secure contents never surface.
    filled: bool = False
    value: str = ""
    # Whether the control is in a selected/active state (XCUITest `selected` trait).
    # Surfaced so the agent can VERIFY the effect of a tap ("I selected X — is X now
    # active?") instead of re-tapping blindly — the fix for filter/tab/segment fixation.
    selected: bool = False
    # On/off state of a two-state control (a Switch, or anything with the ToggleButton
    # trait), read from the app's own value. None = not a two-state control. Shown to the
    # agent as "(on)"/"(off)" so it never has to judge a switch from the screenshot (an
    # agent once passed "the switch is on" while the tree said value="0").
    toggled: bool | None = None
    # The developer-set accessibility identifier (XML `name`) when it differs from the label:
    # the most stable way to find this element again (not localized, not read aloud).
    identifier: str = ""
    # Occlusion handling: a control drawn on top (e.g. a floating "Save" CTA/tooltip)
    # can cover this element's centre, so a centre-tap hits the overlay, not this. The
    # perceiver computes an un-covered tap point (tap_x/tap_y) when it can, and names
    # the covering element in `covered_by` so the brain knows to scroll it clear.
    tap_x: float | None = None
    tap_y: float | None = None
    covered_by: str = ""
    # A control cut off at the left/right screen edge signals a horizontally-scrollable
    # row (chip bar, carousel, tab strip) with MORE items off-screen — so the agent
    # knows to swipe sideways to reveal them instead of concluding a target is absent.
    edge_cropped: str = ""  # "" | "left" | "right"

    @property
    def tap_point(self) -> tuple[float, float]:
        """Where a tap should land — the un-occluded point if computed, else centre."""
        if self.tap_x is not None and self.tap_y is not None:
            return (self.tap_x, self.tap_y)
        return self.frame.center


@dataclass
class ActionSpace:
    """What the brain reasons over: on-screen elements + a serialized view + the
    screen size (so vision/grounding have the coordinate space)."""

    elements: list[Element] = field(default_factory=list)
    text: str = ""
    screen_w: int = 0
    screen_h: int = 0
    source_hash: str = ""  # signature of the screen, to detect no-op actions
    # Harness-provided context for the model (the clause checklist, the agent's own
    # notes), rendered ahead of the history. Written by hooks, never by the perceiver.
    context: str = ""
    # Whether an on-screen keyboard was present in this read — so the harness only asks
    # to hide a keyboard that exists (each hide is a device query that walks the tree).
    keyboard: bool = False
    # Where the on-screen keyboard starts (points from the top), when one is up: a coordinate
    # tap below it lands on a key and types a letter.
    keyboard_top: float = 0.0
    # The loop's step number for this observation — shown to the agent so the evidence
    # step it cites matches the harness's own numbering.
    step: int = 0
    # Things the harness did on the agent's behalf while reading this screen (e.g. accepted
    # an iOS permission prompt). Written by the perceiver; a hook puts them in the history.
    harness_notes: list[str] = field(default_factory=list)
    # Title of an APP dialog (alert / action sheet) on screen, if any — replay must never judge
    # a check underneath a dialog the recording did not have.
    alert: str = ""

    def by_ref(self, ref: str) -> Element | None:
        return next((e for e in self.elements if e.ref == ref), None)


class DeviceProvider(Protocol):
    """Which device — the portability seam. Returns a started DeviceDriver. A
    SimulatorProvider boots a local sim; a SauceProvider connects to a farm."""

    async def driver(self, spec: Any) -> "DeviceDriver": ...
    async def cleanup(self) -> None: ...


class DeviceDriver(Protocol):
    """Intent → iOS actions, and the artifact owner. Pure actuation — no decisions.

    `accessibility()` returns the raw on-screen hierarchy the Perceiver parses;
    tap/type/swipe act on a grounded Element; the rest are evidence."""

    async def start(self) -> None: ...
    async def tap(self, element: Element) -> None: ...
    async def type(self, element: Element, text: str, clear: bool = False) -> None: ...
    async def back(self) -> None: ...                   # system back (left-edge pan)
    async def swipe(self, direction: str) -> None: ...  # "up"|"down"|"left"|"right"
    async def hide_keyboard(self) -> bool: ...          # dismiss keyboard (keeps a11y tree small)
    async def accessibility(self) -> str: ...           # raw source (XML) for the Perceiver
    async def screenshot(self) -> str: ...              # artifact path (PNG)
    def screen_size(self) -> tuple[int, int]: ...
    async def dismiss_alert(self) -> str | None: ...    # accept an OS dialog; its text, or None
    def logs(self) -> tuple[list[str], list[str]]: ...  # (device_errors, crashes)
    async def stop(self) -> str | None: ...             # returns recorded video path


class Perceiver(Protocol):
    """Screen → LLM-readable action space with coordinate-grounded refs."""

    async def observe(self, driver: DeviceDriver) -> ActionSpace: ...


class VisionPolicy(Protocol):
    def should_see(self, space: ActionSpace) -> bool: ...


@dataclass(frozen=True)
class GroundedElement:
    """A tappable target found by VISION where the accessibility tree was blind —
    an icon-only chip the tree mislabels, or content in a dead zone the tree omits.
    `frame` is in screen points (same space as Element.frame), so the driver grounds
    it identically. `confidence` lets the caller drop low-certainty detections."""

    label: str
    frame: Frame
    confidence: float = 1.0


class Grounder(Protocol):
    """Vision fallback for grounding: given a screenshot (optionally cropped to the
    low-information region) plus the current a11y space, return tappable targets the
    tree missed. This is the seam where a specialist grounding model (UGround,
    Ferret-UI Lite) plugs in; the default NullGrounder returns nothing, so a11y-only
    behavior is unchanged until a real model is wired and proven on the offline eval."""

    async def detect(
        self,
        screenshot_png: bytes,
        region: tuple[float, float, float, float] | None,
        space: ActionSpace,
    ) -> list[GroundedElement]: ...


class ActingSession(Protocol):
    """What graduation's replay drives: read the screen, perform one action, capture.
    The iOS DeviceSession is one; a second platform's session implements the same four."""

    act_timed_out: bool   # the last act() hit the device timeout (its effect is unknown)

    async def observe(self, heartbeat: Any = None) -> ActionSpace: ...

    async def act(self, space: ActionSpace, action: Any) -> str: ...

    async def screenshot(self) -> bytes | None: ...


SCROLL_SHIFT_PT = 4.0  # a row that moved less than this did not scroll (sub-pixel jitter)


def screen_moved(before: ActionSpace, after: ActionSpace) -> bool:
    """Did the content SCROLL between two reads? Rows that appear on both moved position, or
    every row was replaced. Not "did anything change": a counter updating in place ("Loaded:
    40 items" -> "60") or rows appended out of sight is not movement (a self-loading
    catalog looked like a moving list, so scroll_to never noticed it sat at the top)."""
    old = {(e.type, e.label): e.frame.y for e in before.elements if e.label}
    new = {(e.type, e.label): e.frame.y for e in after.elements if e.label}
    if any(abs(old[k] - y) > SCROLL_SHIFT_PT for k, y in new.items() if k in old):
        return True     # a row seen on both reads moved
    # A flick can carry every row out of view (only a fixed header is on both reads): rows
    # left AND others arrived. One label swapping in place is a counter, not a scroll.
    return len(old.keys() - new.keys()) >= 2 and len(new.keys() - old.keys()) >= 2

