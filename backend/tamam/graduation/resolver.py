"""Find a script step's target on the current screen — exactly one element, or none.

Pure. Locators are tried best first (accessibility identifier, then type + label). Several
matches inside the content are never disambiguated by position — lists reorder, so a guess
could tap the wrong row; the step is ambiguous and replay escalates to a full AI run.

The one exception is screen chrome: the navigation bar and the tab bar sit at fixed places
and do not reorder. A "Shop" back button (top) and a "Shop" tab (bottom) are different
controls; if exactly one match is in the same bar the recorded tap was in, that is the one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tamam.graduation.script import Target

_TOP_BAR, _BOTTOM_BAR = 0.13, 0.87    # centre-y fractions: navigation bar / tab bar

@dataclass
class Resolution:
    element: Any | None
    how: str            # "identifier" | "type_label" | "nearest" | "none" | "ambiguous"
    candidates: int = 0


def resolve(target: Target, space: Any) -> Resolution:
    els = list(getattr(space, "elements", []))
    for loc in target.locators:
        if loc.kind == "identifier":
            found = [e for e in els if getattr(e, "identifier", "") == loc.value]
        else:
            typ, _, label = loc.value.partition("|")
            found = [e for e in els if e.type == typ and (e.label or "") == label]
        if len(found) == 1:
            return Resolution(found[0], loc.kind, 1)
        if len(found) > 1:
            # Several identical controls (an "Add to cart" per row): position is not identity
            # (lists reorder), so never guess — the step escalates to a full AI run.
            same_bar = _in_recorded_bar(found, target, space)
            if same_bar is not None:
                return Resolution(same_bar, "bar", len(found))
            outer = _one_control(found)
            if outer is not None:
                return Resolution(outer, "nested", len(found))
            return Resolution(None, "ambiguous", len(found))
    return Resolution(None, "none", 0)


def _bar(centre_y: float) -> str | None:
    return "top" if centre_y < _TOP_BAR else "bottom" if centre_y > _BOTTOM_BAR else None


def _in_recorded_bar(found: list, target: Target, space: Any) -> Any | None:
    """The single match in the bar the recorded tap was in — else None (no guess)."""
    ff = target.frame_frac
    h = getattr(space, "screen_h", 0) or 0
    if len(ff) != 4 or not h:
        return None
    bar = _bar(ff[1] + ff[3] / 2)
    if bar is None:
        return None
    hits = [e for e in found if _bar((e.frame.y + e.frame.height / 2) / h) == bar]
    return hits[0] if len(hits) == 1 else None


def _one_control(found: list) -> Any | None:
    """The outermost match when every other match lies INSIDE it: a merged row and the text
    within it are one control, not two rows to choose between (#26 Feedback: the 'I agree' row
    and its own label share the name; replay called it ambiguous on a correct build). List rows
    never nest, so this is not a positional guess."""
    def inside(a: Any, b: Any) -> bool:
        fa, fb = a.frame, b.frame
        return (fb.x - 1 <= fa.x and fb.y - 1 <= fa.y and fa.x + fa.width <= fb.x + fb.width + 1
                and fa.y + fa.height <= fb.y + fb.height + 1)
    for cand in sorted(found, key=lambda e: -(e.frame.width * e.frame.height)):
        if all(e is cand or inside(e, cand) for e in found):
            return cand
    return None

