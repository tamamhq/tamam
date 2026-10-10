"""IOSPerceiver — screen → indexed ActionSpace from the XCUITest accessibility tree.

Appium/WebDriverAgent expose the on-screen hierarchy as XML (element type, name,
label, value, frame, visible/enabled). We parse it into viewport-scoped,
coordinate-grounded Elements — the iOS analog of the web ViewportPerceiver, same
discipline: only what's on screen, so the agent must scroll to discover.

`parse_source` is pure and unit-tested on a captured fixture; only `observe`
touches a live device.
"""

from __future__ import annotations

import hashlib
import logging
import re
import xml.etree.ElementTree as ET
from typing import Any

from tamam.executors.core.contracts import ActionSpace, Element, Frame

logger = logging.getLogger(__name__)

# Types worth acting on / counting. Inputs are kept even when unlabeled.
# NOTE: keyboard "Key" elements are deliberately EXCLUDED. The agent enters text via the
# `type` action (which drives the focused field), never by tapping individual keys — so on
# any keyboard-up screen (notably rich-text editors) the ~30+ letter/space/delete keys
# were pure noise that buried the real controls (title, blocks) and drove the agent into
# tap-the-wrong-thing loops. Functional keyboard controls exposed as Buttons ("return",
# "shift", "Hide keyboard") are unaffected — only the raw Keys are dropped.
INTERACTIVE_TYPES = {
    "Button", "Cell", "TextField", "SecureTextField", "SearchField", "TextView", "Link",
    "Switch", "Slider", "SegmentedControl", "MenuItem", "MenuButton",
    "TabBarButton", "PickerWheel", "StaticText", "Image",
}
INPUT_TYPES = {"TextField", "SecureTextField", "SearchField", "TextView", "PickerWheel", "Slider", "Switch"}
# Free-text inputs whose empty/filled state must be surfaced to the brain: an empty
# field otherwise looks identical to a full one, so the agent can't tell a required
# field wasn't populated before submitting (the silent-login-failure class of bug).
# TextView: a multi-line input (UITextView, a React Native multiline TextInput, SwiftUI's
# TextEditor) — it was dropped, and an agent spent 10 steps tap_pointing at a box it
# could see but never got a ref for.
TEXT_INPUT_TYPES = {"TextField", "SecureTextField", "SearchField", "TextView"}


# An actionable ancestor makes a nested PASSIVE label redundant: tapping the container is
# the action, and iOS aggregates the child text into the container's own label. Emitting both
# the Cell and its inner StaticText/Image is pure noise (token bloat) and tap ambiguity (two
# refs, same effect). We drop the passive child ONLY when an emitted actionable ancestor
# already conveys its text — never a child that carries text the container doesn't, and never
# an independently-actionable child (a Button inside a Cell stays).
_CONTAINER_TYPES = {"Button", "Cell", "Link", "TabBarButton", "MenuItem", "MenuButton", "SegmentedControl",
                    "Switch", "Other"}


def _is_touchable_other(el: ET.Element, frame, screen_h: int) -> bool:
    """React Native (and other cross-platform) touchables with no accessibility role reach
    XCUITest as an ACCESSIBLE `Other` that carries the label — the only element for a button
    the user can tap (on a React Native app, every row of a home menu). Keep one when iOS marks it
    accessible, it is named, it is not a full-screen wrapper, and it wraps no real control of
    its own (then it is a container and its controls are listed instead)."""
    if el.get("accessible") != "true" or not (el.get("label") or el.get("name") or "").strip():
        return False
    if screen_h and frame.height >= 0.9 * screen_h:
        return False
    for d in el.iter():
        if d is el:
            continue
        if _short_type(d.get("type") or d.tag) in _CONTROL_TYPES and \
                (d.get("label") or d.get("name") or d.get("accessible") == "true"):
            return False
    return True
_SUBSUMABLE_TYPES = {"StaticText", "Image"}
# Real controls: an accessible "Other" wrapping one of these is a container, not a control.
_CONTROL_TYPES = (INTERACTIVE_TYPES - _SUBSUMABLE_TYPES) | {"Other"}


def _short_type(raw: str) -> str:
    return raw.replace("XCUIElementType", "") if raw else ""


def _input_state(el: ET.Element, etype: str) -> tuple[bool, str]:
    """(is_filled, display_value) for a text input. iOS reports an EMPTY field's
    `value` as its placeholder, so compare against `placeholderValue` (the reliable
    signal) plus label/name — a value that just echoes any of those is empty.
    SecureTextField contents are NEVER returned — only the flag."""
    placeholder = (el.get("placeholderValue") or "").strip()
    label_attr = (el.get("label") or "").strip()
    name_attr = (el.get("name") or "").strip()
    raw = (el.get("value") or "").strip()
    filled = bool(raw) and raw not in (placeholder, label_attr, name_attr)
    if not filled:
        return False, ""
    if etype == "SecureTextField":
        return True, ""  # security: never surface secure-field contents
    return True, raw[:120]


_TOGGLE_ON = {"1", "true", "on"}
_TOGGLE_OFF = {"0", "false", "off"}
# Two-state controls whose inner part is the real hit target: SwiftUI exposes a labelled
# row-wide Switch wrapping an unlabelled knob Switch. A tap on the row's centre lands on
# the label and does nothing, so the knob is where a tap must go. Switch only: a tap on a
# Slider SETS its value, so redirecting one would change what the agent meant to do.
_NESTED_CONTROL_TYPES = {"Switch"}


def _toggle_state(el: ET.Element, etype: str) -> bool | None:
    """On/off for a two-state control, from the app's own `value`; None otherwise."""
    if etype != "Switch" and "ToggleButton" not in (el.get("traits") or ""):
        return None
    raw = (el.get("value") or "").strip().lower()
    if raw in _TOGGLE_ON:
        return True
    if raw in _TOGGLE_OFF:
        return False
    return None


def _row_name(el: ET.Element, parent_map: dict) -> str:
    """A name for an unlabelled control from the row it sits in. UIKit settings rows are
    `Cell "Email Notifications" > Button "Email Notifications" + Switch (no label)`: the
    switch's meaning is its row's. Nearest named Cell ancestor first, then a labelled
    sibling text/button in the same parent. Never guesses across rows."""
    parent = parent_map.get(el)
    anc = parent
    while anc is not None:
        if _short_type(anc.get("type") or anc.tag) == "Cell":
            name = ((anc.get("label") or "").strip() or (anc.get("name") or "").strip())
            if name:
                return name[:120]
            break  # the nearest Cell is the row; a name further up belongs to the list
        anc = parent_map.get(anc)
    if parent is not None:
        # Same ROW only: a sibling that overlaps the control vertically, nearest first. A flat
        # container holds a whole form (Flutter: every widget is a child of one node), and
        # "the first labelled sibling" named both of its switches after the form's first
        # label, "Date".
        me = _frame_of(el)
        best: tuple[float, str] | None = None
        for sib in parent:
            if sib is el or _short_type(sib.get("type") or sib.tag) not in ("StaticText", "Button"):
                continue
            name = (sib.get("label") or "").strip() or (sib.get("name") or "").strip()
            if not name:
                continue
            f = _frame_of(sib)
            if me is None or f is None:
                return name[:120]           # no geometry to judge rows by: the old behaviour
            if f.y >= me.y + me.height or me.y >= f.y + f.height:
                continue                    # another row
            gap = max(f.x - (me.x + me.width), me.x - (f.x + f.width), 0)
            if best is None or gap < best[0]:
                best = (gap, name[:120])
        if best is not None:
            return best[1]
    return ""


def _identifier_of(el: ET.Element, label: str) -> str:
    """The accessibility identifier when the app set one distinct from what is shown. iOS
    reports `name` = the identifier if set, else the label; equal to the label (or the
    value) means no separate identifier."""
    name = (el.get("name") or "").strip()
    if not name or name == label or name == (el.get("label") or "").strip() \
            or name == (el.get("value") or "").strip():
        return ""
    return name[:120]


def _label_of(el: ET.Element) -> str:
    for key in ("label", "name", "value"):
        v = (el.get(key) or "").strip()
        if v:
            return v[:120]
    return ""


def _frame_of(el: ET.Element) -> Frame | None:
    try:
        return Frame(
            x=float(el.get("x", "0")), y=float(el.get("y", "0")),
            width=float(el.get("width", "0")), height=float(el.get("height", "0")),
        )
    except (TypeError, ValueError):
        return None


# Control types that would capture a tap if drawn over another element's centre.
# Kept narrow (buttons) so we only flag genuine tap-stealing overlays — a FAB, a
# "Save" CTA, a tooltip's dismiss button — never a text label that sits within a cell.
# How much of a control must lie past a side edge before it reads as "this row scrolls
# sideways": a touch area overhanging the margin by a few points (an icon button pinned to
# the right edge) is not a clipped chip.
_EDGE_MIN_HIDDEN_PT = 12
_EDGE_MIN_HIDDEN_FRAC = 0.2


def _edge_cut(frame: Frame, screen_w: float) -> str:
    """'right'/'left' when a control is clipped by ONE side edge (a horizontally scrolling row
    with more off-screen), else ''. Something past both edges is a full-bleed backdrop."""
    right = frame.x + frame.width - screen_w
    left = -frame.x
    if right > 1 and left > 1:
        return ""
    need = max(_EDGE_MIN_HIDDEN_PT, _EDGE_MIN_HIDDEN_FRAC * frame.width)
    if frame.x < screen_w and right >= need:
        return "right"
    if frame.x + frame.width > 0 and left >= need:
        return "left"
    return ""


_OVERLAY_TYPES = {"Button", "MenuButton", "TabBarButton"}
_MIN_CLEAR_GAP = 8.0  # px of free band needed before we redirect a tap into it


def _clear_tap_point(t: Frame, o: Frame) -> tuple[float, float] | None:
    """The midpoint of the largest side-band of `t` NOT covered by overlay `o`, or
    None if `o` leaves no usable band (t is effectively fully covered)."""
    cx, cy = t.center
    gaps = {
        "left": o.x - t.x,
        "right": (t.x + t.width) - (o.x + o.width),
        "top": o.y - t.y,
        "bottom": (t.y + t.height) - (o.y + o.height),
    }
    side = max(gaps, key=gaps.get)
    if gaps[side] < _MIN_CLEAR_GAP:
        return None
    if side == "left":
        return (t.x + gaps["left"] / 2, cy)
    if side == "right":
        return (o.x + o.width + gaps["right"] / 2, cy)
    if side == "top":
        return (cx, t.y + gaps["top"] / 2)
    return (cx, o.y + o.height + gaps["bottom"] / 2)


def _subsume_nested(items: list[dict], parent_map: dict) -> list[dict]:
    """Drop passive children (StaticText/Image) nested inside an emitted actionable container
    whose label already contains the child's text — the container is the tap target, so the
    child is a redundant duplicate. Conservative by design: a child whose label is NOT already
    in an actionable ancestor's label is KEPT (no information loss), and non-passive children
    (an inner Button, etc.) are never touched. Operates on the tree (parent_map), so it never
    merges elements that merely overlap spatially."""
    emitted = {it["_el"]: it for it in items}
    kept: list[dict] = []
    for it in items:
        if it["type"] in _SUBSUMABLE_TYPES and it["label"]:
            child_label = it["label"].lower()
            anc = parent_map.get(it["_el"])
            redundant = False
            while anc is not None:
                a = emitted.get(anc)
                if a is not None and a["type"] in _CONTAINER_TYPES and a["label"] \
                        and child_label in a["label"].lower():
                    redundant = True
                    break
                anc = parent_map.get(anc)
            if redundant:
                continue
        kept.append(it)
    return kept


def _merge_nested_controls(items: list[dict], parent_map: dict) -> list[dict]:
    """Fold an inner control into the same-type control that contains it (a Switch row
    wrapping its knob). The two are one control: emitting both gave the agent a second,
    meaningless target labelled only by its raw value ("0"). The outer one keeps the
    label; it takes the inner one's state when it has none, and the inner one's centre
    as its tap point, since that is where a tap actually flips it."""
    emitted = {it["_el"]: it for it in items}
    inner_count: dict[int, int] = {}
    for it in items:
        if it["type"] in _NESTED_CONTROL_TYPES:
            anc = parent_map.get(it["_el"])
            while anc is not None:
                a = emitted.get(anc)
                if a is not None and a["type"] == it["type"]:
                    inner_count[id(a)] = inner_count.get(id(a), 0) + 1
                    break
                anc = parent_map.get(anc)
    kept: list[dict] = []
    for it in items:
        if it["type"] in _NESTED_CONTROL_TYPES:
            anc = parent_map.get(it["_el"])
            outer = None
            while anc is not None:
                a = emitted.get(anc)
                if a is not None and a["type"] == it["type"]:
                    outer = a
                    break
                anc = parent_map.get(anc)
            if outer is not None and inner_count.get(id(outer)) == 1:
                if outer["toggled"] is None:
                    outer["toggled"] = it["toggled"]
                of, f = outer["frame"], it["frame"]
                inside = (of.x <= f.x and f.x + f.width <= of.x + of.width
                          and of.y <= f.y and f.y + f.height <= of.y + of.height)
                if inside and f.width * f.height < of.width * of.height:
                    outer["tap_x"], outer["tap_y"] = f.center
                    outer["knob"] = True
                continue
        kept.append(it)
    return kept


def _row_switch_tap(items: list[dict]) -> None:
    """A row-wide Switch with no separate knob element (some iOS versions) still flips only
    at its knob, which iOS draws at the row's trailing edge. Tap there, not the label in
    the middle. (Left-to-right layouts; a right-to-left row would need the leading edge.)"""
    for it in items:
        f: Frame = it["frame"]
        if it["type"] == "Switch" and not it.get("knob") and f.width > 3 * f.height:
            it["tap_x"], it["tap_y"] = f.x + f.width - min(40.0, f.width / 4), f.center[1]
            it["knob"] = True


def _resolve_occlusion(items: list[dict]) -> None:
    """Mark each item covered by a later-drawn, smaller control over its centre, and
    compute an un-occluded tap point when one exists. `items` is in document order
    (later index == drawn on top); mutates items in place."""
    for i, t in enumerate(items):
        tf: Frame = t["frame"]
        # A control's real hit point (a switch's knob) is where coverage matters.
        cx, cy = (t["tap_x"], t["tap_y"]) if t.get("knob") else tf.center
        t_area = tf.width * tf.height
        best = None  # smallest-area qualifying overlay (topmost-looking)
        for j in range(i + 1, len(items)):
            o = items[j]
            if o["type"] not in _OVERLAY_TYPES:
                continue
            of: Frame = o["frame"]
            o_area = of.width * of.height
            if o_area >= t_area:
                continue  # a larger element is a container/ancestor, not an overlay
            if not (of.x <= cx <= of.x + of.width and of.y <= cy <= of.y + of.height):
                continue  # doesn't cover the centre
            if best is None or o_area < best[1]:
                best = (j, o_area, of)
        if best is None:
            continue
        j, _, of = best
        t["covered_idx"] = j
        if t.get("knob"):
            continue  # report it; never move a tap off the knob onto the label
        pt = _clear_tap_point(tf, of)
        if pt is not None:
            t["tap_x"], t["tap_y"] = pt


KEYBOARD = "the keyboard"


def _mark_under_keyboard(items: list[dict], root: ET.Element, screen_h: int) -> None:
    """An element whose centre lies under the on-screen keyboard cannot be tapped — a tap
    there lands on a key and types a letter (a wizard's next field and its button sat
    under the keyboard for 30 steps). Mark it, so the agent hides the keyboard first; the
    keyboard's own controls (return, shift, Done) are never marked."""
    kb = next((e for e in root.iter() if _short_type(e.get("type") or e.tag) == "Keyboard"
               and e.get("visible") != "false"), None)
    f = _frame_of(kb) if kb is not None else None
    if f is None or f.height <= 0 or (screen_h and f.y >= screen_h):
        return
    inside = set(kb.iter())
    for it in items:
        if it.get("_el") in inside:
            continue
        fr = it["frame"]
        cy = fr.y + fr.height / 2
        if cy >= f.y and fr.x + fr.width > f.x and fr.x < f.x + f.width:
            it["under_keyboard"] = True


def keyboard_top(source: str) -> float:
    """The y (points) where a visible on-screen keyboard starts, or 0 when there is none."""
    if "XCUIElementTypeKeyboard" not in source:
        return 0.0
    try:
        root = ET.fromstring(source)
    except ET.ParseError:
        return 0.0
    for e in root.iter():
        if _short_type(e.get("type") or e.tag) == "Keyboard" and e.get("visible") != "false":
            f = _frame_of(e)
            if f is not None and f.height > 0:
                return f.y
    return 0.0


def parse_source(source: str, screen_w: int, screen_h: int) -> list[Element]:
    """Pure: XCUITest XML → viewport-scoped, coordinate-grounded Elements."""
    try:
        root = ET.fromstring(source)
    except ET.ParseError:
        return []
    parent_map = {child: parent for parent in root.iter() for child in parent}
    # Document order is kept (append order): it is the z-order proxy for occlusion.
    items = [c for el in root.iter()
             if (c := _candidate(el, parent_map, screen_w, screen_h)) is not None]

    # Drop passive children already conveyed by an actionable ancestor (Cell > StaticText)
    # before occlusion + ref assignment, so refs stay dense and the brain sees one target.
    # Order matters: fold nested controls first (they set the outer one's state and tap
    # point), then drop passive children, then resolve occlusion on the final set.
    items = _merge_nested_controls(items, parent_map)
    _row_switch_tap(items)
    items = _subsume_nested(items, parent_map)

    _resolve_occlusion(items)  # in document (z) order, before reading-order sort
    _mark_under_keyboard(items, root, screen_h)
    return _to_elements(items)


def _candidate(el: ET.Element, parent_map: dict, screen_w: int, screen_h: int) -> dict | None:
    """One XML node as an element the agent could act on, or None: invisible, not interactive,
    off screen, or nameless."""
    if el.get("visible") not in (None, "true") or el.get("visible") == "false":
        return None
    etype = _short_type(el.get("type") or el.tag)
    if etype not in INTERACTIVE_TYPES and etype != "Other":
        return None
    frame = _frame_of(el)
    if frame is None or frame.width <= 0 or frame.height <= 0:
        return None
    if etype == "Other" and not _is_touchable_other(el, frame, screen_h):
        return None
    # viewport scoping: any part on screen
    if frame.y >= screen_h or frame.y + frame.height <= 0:
        return None
    if frame.x >= screen_w or frame.x + frame.width <= 0:
        return None
    label = _label_of(el)
    toggled = _toggle_state(el, etype)
    if etype == "Switch" and toggled is not None \
            and label == (el.get("value") or "").strip()[:120]:
        # A switch "named" by its own value ("0") has no name: take its row's.
        label = _row_name(el, parent_map)
    filled = False
    value = ""
    if etype in TEXT_INPUT_TYPES:
        # Prefer the stable identity (label/name) over the entered value so the
        # field's name stays put; expose fill-state + value separately.
        ident = (el.get("label") or el.get("name") or "").strip()
        if ident:
            label = ident
        filled, value = _input_state(el, etype)
    if not label and etype not in INPUT_TYPES:
        return None
    return {
        "type": etype, "label": label, "frame": frame,
        "enabled": (el.get("enabled") in (None, "true")),
        "filled": filled, "value": value,
        "selected": el.get("selected") == "true",
        "toggled": toggled,
        "identifier": _identifier_of(el, label),
        # A control clipped by a side edge → a horizontally-scrollable row with more
        # off-screen. (Vertical clipping is handled by ordinary scrolling; only the
        # non-obvious horizontal case is flagged.)
        "edge_cropped": _edge_cut(frame, screen_w),
        "tap_x": None, "tap_y": None, "covered_idx": None,
        "_el": el,
    }


def _to_elements(items: list[dict]) -> list[Element]:
    """Final elements in reading order (top to bottom, left to right), refs e1, e2, …"""
    order = sorted(range(len(items)), key=lambda k: (items[k]["frame"].y, items[k]["frame"].x))
    ref_of = {k: f"e{i+1}" for i, k in enumerate(order)}

    def covered_by(it: dict) -> str:
        if it.get("under_keyboard"):
            return KEYBOARD
        return ref_of.get(it["covered_idx"], "") if it["covered_idx"] is not None else ""

    return [
        Element(
            ref=ref_of[k], type=it["type"], label=it["label"], frame=it["frame"],
            enabled=it["enabled"], filled=it["filled"], value=it["value"],
            selected=it["selected"], toggled=it["toggled"], identifier=it["identifier"],
            edge_cropped=it["edge_cropped"], tap_x=it["tap_x"], tap_y=it["tap_y"],
            covered_by=covered_by(it),
        )
        for k in order for it in [items[k]]
    ]


# A control clipped by a side edge proves its row continues off-screen; this maps the
# clipped edge to the swipe that reveals the rest. Used both to tell the agent (serialize)
# and to hold it to it (the executor's premature-conclusion guard) — one definition, so
# the advice and the enforcement can never drift apart.
REVEAL_SWIPE = {"right": "left", "left": "right"}


def unrevealed_directions(elements: list[Element]) -> set[str]:
    """Swipe directions that would bring genuinely unseen content into view.

    Empty when nothing on screen is clipped by a side edge — i.e. when there is no
    evidence that any content is being withheld from the agent."""
    return {REVEAL_SWIPE[e.edge_cropped] for e in elements if e.edge_cropped in REVEAL_SWIPE}


def is_opaque_label(label: str) -> bool:
    """True when a label names the ASSET rather than what the control does.

    Icon-only controls surface their image name — `square.grid.2x2`, `shippingbox`,
    `ic_menu_search` — which says nothing about their meaning, so the agent can only
    guess. A real word ("Search", "Add to Cart") is not opaque. The test is
    deliberately narrow: one token, and either dotted (a symbol path) or all
    lower-case (an asset slug); a capitalised single word is a normal label."""
    text = label.strip()
    if not text or " " in text:
        return False
    return "." in text or text.islower()


def revealed_label(probed: Element, after: list[Element]) -> str | None:
    """What an opaque control turned out to be, once selecting it made it say so.

    Many icon-only controls (chips, tabs, segments) render their name ONLY while
    selected. After tapping one, the element in that same spot may now carry a real
    label — that is the control's meaning, learned by doing rather than by reading."""
    py = probed.frame.y
    px = probed.frame.x
    best, best_dx = None, None
    for e in after:
        if abs(e.frame.y - py) > 15:          # a different row entirely
            continue
        if is_opaque_label(e.label) or not e.label.strip():
            continue
        dx = abs(e.frame.x - px)
        if dx > max(60.0, probed.frame.width):  # not where the probed control sat
            continue
        if best_dx is None or dx < best_dx:
            best, best_dx = e.label.strip(), dx
    return best


def serialize(elements: list[Element]) -> str:
    out = []
    for e in elements:
        line = f'[{e.ref}] {e.type} "{e.label}"'
        if e.type in TEXT_INPUT_TYPES:
            # So the brain can see a required field is still blank before submitting.
            if not e.filled:
                line += " (empty)"
            elif e.type == "SecureTextField":
                line += " (filled)"      # never the contents
            else:
                line += f' = "{e.value}"'
        if e.selected:
            # So the agent can verify a tap's effect (is the thing I picked now active?).
            line += " (selected)"
        if e.toggled is not None:
            # Read from the app, not the pixels: a switch's state is never a visual guess.
            line += " (on)" if e.toggled else " (off)"
        if e.edge_cropped:
            swipe = REVEAL_SWIPE[e.edge_cropped]
            line += (f" (cut off at the {e.edge_cropped} edge — this row scrolls sideways; "
                     f"swipe {swipe} WITH ref={e.ref} to reveal more items)")
        if e.covered_by == KEYBOARD:
            line += " (under the keyboard — hide_keyboard first, then tap it)"
        elif e.covered_by:
            # Tell the brain the target is under an overlay so it can scroll it clear.
            line += f" (covered by {e.covered_by} — scroll it into the clear before tapping)"
        if not e.enabled:
            line += " (disabled)"
        out.append(line)
    return "\n".join(out)


def screen_signature(elements: list[Element]) -> str:
    """A STABLE, content-based screen signature for change detection (NoProgress / the
    'screen changed' note). Hashes the MEANINGFUL element state — type, label, fill state,
    value, selected, enabled — with a real hash (not Python's per-process-salted `hash()`,
    which was non-reproducible across runs). Deliberately excludes refs (parse-order noise)
    and pixel frames, and ignores raw-XML volatility (a blinking cursor / ticking clock that
    isn't an element label), so 'no change' means the interactive screen genuinely didn't
    advance — the signal the guardrails actually want."""
    basis = "\n".join(
        f"{e.type}|{e.label or ''}|{int(e.filled)}|{e.value or ''}|{int(e.selected)}|{int(e.enabled)}"
        f"|{'' if e.toggled is None else int(e.toggled)}"
        for e in elements
    )
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


_ROOT_APP = re.compile(r"<XCUIElementTypeApplication\b[^>]*>")
_ALERT = re.compile(r"<XCUIElementTypeAlert\b[^>]*>")
_ATTR = re.compile(r'\b(name|label)="([^"]*)"')


def system_dialog_title(source: str) -> str | None:
    """The title of an iOS SYSTEM dialog (permission prompt etc.) this read shows, else None.

    With respectSystemAlerts on, WebDriverAgent returns SpringBoard's tree instead of the
    app's while a system dialog is up. SpringBoard's root carries a blank name, the app's
    root its display name — so "blank root + an Alert" is a system dialog, and an Alert
    under a named root is the APP's own dialog, which is left to the agent."""
    root = _ROOT_APP.search(source or "")
    if not root:
        return None
    attrs = dict(_ATTR.findall(root.group(0)))
    if (attrs.get("name") or attrs.get("label") or "").strip():
        return None  # the app under test (or another named app) is on screen
    alert = _ALERT.search(source)
    if not alert:
        return None  # home screen / SpringBoard without a dialog
    a = dict(_ATTR.findall(alert.group(0)))
    return (a.get("label") or a.get("name") or "").strip() or "(system dialog)"


_SHEET = re.compile(r"<XCUIElementTypeSheet\b[^>]*>")


def app_dialog_title(source: str) -> str:
    """The title of an alert / action sheet the APP is showing (system dialogs are handled
    before this), or ""."""
    if not source or system_dialog_title(source) is not None:
        return ""
    m = _ALERT.search(source) or _SHEET.search(source)
    if not m:
        return ""
    a = dict(_ATTR.findall(m.group(0)))
    return (a.get("label") or a.get("name") or "").strip() or "(dialog)"


class IOSPerceiver:
    """Reads the screen. iOS system dialogs are not part of the app under test: one showing
    is accepted here (what WebDriverAgent's autoAcceptAlerts used to do blindly for every
    alert) and the space carries a note so the agent knows the harness acted for it."""

    MAX_SYSTEM_DIALOGS = 3  # permission prompts can come back to back; bounded

    def __init__(self, *, accept_system_dialogs: bool = True) -> None:
        self.accept_system_dialogs = accept_system_dialogs

    async def observe(self, driver: Any) -> ActionSpace:
        source = await driver.accessibility()
        notes: list[str] = []
        for _ in range(self.MAX_SYSTEM_DIALOGS if self.accept_system_dialogs else 0):
            title = system_dialog_title(source)
            if title is None:
                break
            accepted = await driver.dismiss_alert()
            if not accepted:
                break  # could not accept it — show it to the agent rather than loop
            logger.info("accepted iOS system dialog %r", title[:120])
            notes.append(f'[harness] iOS showed a system dialog "{title[:120]}" and it was '
                         f'accepted automatically')
            source = await driver.accessibility()
        self.last_source = source  # kept for the per-step evidence trace
        w, h = driver.screen_size()
        elements = parse_source(source, w, h)
        return ActionSpace(
            elements=elements, text=serialize(elements),
            screen_w=w, screen_h=h, source_hash=screen_signature(elements),
            # A keyboard element can stay in the tree after it slid off the bottom (its top
            # below the screen): that keyboard is down.
            keyboard=0 < (kb := keyboard_top(source)) < h,
            keyboard_top=kb if 0 < kb < h else 0.0,
            harness_notes=notes,
            alert=app_dialog_title(source),
        )
