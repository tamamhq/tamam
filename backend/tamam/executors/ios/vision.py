"""Set-of-Marks for iOS + the policy for when to use it.

Because the perceiver already carries each element's frame, drawing the marks is
pure image work — no per-element locating (unlike web). Vision is the fallback for
what the accessibility tree can't express (unlabeled/visual-only UI).
"""

from __future__ import annotations

import io
import re
from pathlib import Path

from tamam.executors.core.contracts import ActionSpace

# Controls the agent taps to act (not decorative Images/labels). Icon-only controls
# among these mean the accessibility tree is uninformative and vision should help.
_CONTROL_TYPES = {"Button", "MenuButton", "TabBarButton", "SegmentedControl"}
# An SF-Symbol / identifier label like "square.grid.2x2", "tag.circle" — dotted,
# lowercase, no spaces: not a human-readable name, so the brain can't tell it apart.
_SYMBOLISH = re.compile(r"^[a-z0-9]+(?:\.[a-z0-9]+)+$")


def _uninformative(label: str) -> bool:
    text = (label or "").strip()
    return not text or bool(_SYMBOLISH.match(text))


# Coverage grid: how much of the viewport has NO accessible element over it. A large
# empty band is the signal that the a11y tree is missing content (custom-drawn /
# WebView / canvas / RN-without-a11y views expose nothing) — not that the screen is
# genuinely sparse. We ignore the status-bar band at the very top and the tab/nav band
# at the very bottom, which are chrome, and measure the content region in between.
_GRID_COLS = 8
_GRID_ROWS = 16
_TOP_CHROME_FRAC = 0.06     # status bar / notch band, excluded from the content region
_BOTTOM_CHROME_FRAC = 0.10  # home indicator / tab bar band, excluded


def _content_dead_zone(space: ActionSpace) -> float:
    """Fraction of the content region (viewport minus top/bottom chrome bands) NOT
    covered by any accessible element. High = the tree is blind to on-screen content."""
    w, h = space.screen_w, space.screen_h
    if w <= 0 or h <= 0 or not space.elements:
        return 1.0 if not space.elements else 0.0
    y0 = h * _TOP_CHROME_FRAC
    y1 = h * (1.0 - _BOTTOM_CHROME_FRAC)
    cell_w = w / _GRID_COLS
    cell_h = (y1 - y0) / _GRID_ROWS
    if cell_w <= 0 or cell_h <= 0:
        return 0.0
    covered = 0
    total = _GRID_COLS * _GRID_ROWS
    for r in range(_GRID_ROWS):
        cy0 = y0 + r * cell_h
        cy1 = cy0 + cell_h
        for c in range(_GRID_COLS):
            cx0 = c * cell_w
            cx1 = cx0 + cell_w
            # covered if any element's frame overlaps this cell
            for e in space.elements:
                f = e.frame
                if f.x < cx1 and f.x + f.width > cx0 and f.y < cy1 and f.y + f.height > cy0:
                    covered += 1
                    break
    return 1.0 - covered / total


class AXFirstVisionPolicy:
    """Trust the accessibility tree; reach for vision when it can't carry the screen —
    too few elements, the tappable controls are icon-only / unlabeled (e.g. an
    SF-Symbol filter bar) which the text list can't disambiguate, or a large part of
    the content region has NO accessible element at all (the tree is blind to
    custom-drawn / WebView content — the class of bug where a whole page body is
    invisible to the agent while the screenshot shows it plainly)."""

    def __init__(
        self, min_elements: int = 3, poor_ratio: float = 0.5, max_dead_zone: float = 0.6
    ) -> None:
        self._min = min_elements
        self._poor = poor_ratio
        self._max_dead = max_dead_zone

    def should_see(self, space: ActionSpace) -> bool:
        els = space.elements
        if len(els) < self._min:
            return True
        controls = [e for e in els if e.type in _CONTROL_TYPES]
        bad = sum(1 for e in controls if _uninformative(e.label))
        # Absolute: an icon-only toolbar/filter bar (≥3 unlabeled controls) — fire even
        # amid labeled content. Ratio: most controls on screen are unlabeled.
        if bad >= 3:
            return True
        if controls and bad >= 2 and bad / len(controls) >= self._poor:
            return True
        # Dead-zone: most of the content region has no accessible element over it, so
        # the tree can't be describing what's on screen — send the screenshot.
        if _content_dead_zone(space) >= self._max_dead:
            return True
        # A merged container: one big element whose label is several parts run together
        # ("Use …, Email, Password, SIGN IN") — the tree swallowed a whole form, so its fields
        # and labels exist only in the pixels.
        if _merged_container(space):
            return True
        return False


MERGED_PARTS = 3          # comma-joined parts in one label
MERGED_AREA_FRAC = 0.15   # of the screen


def _merged_container(space: ActionSpace) -> bool:
    area = (space.screen_w or 0) * (space.screen_h or 0)
    if area <= 0:
        return False
    return any(len([p for p in (e.label or "").split(", ") if p.strip()]) >= MERGED_PARTS
               and e.frame.width * e.frame.height >= MERGED_AREA_FRAC * area
               for e in space.elements)


def _bbox(frames: list) -> tuple[float, float, float, float]:
    """Union bounding box (x, y, w, h) of a list of frames."""
    x0 = min(f.x for f in frames)
    y0 = min(f.y for f in frames)
    x1 = max(f.x + f.width for f in frames)
    y1 = max(f.y + f.height for f in frames)
    return (x0, y0, x1 - x0, y1 - y0)


def low_info_region(space: ActionSpace) -> tuple[float, float, float, float] | None:
    """The region a vision grounder should focus on — where the a11y tree is weakest.
    Priority: the bounding box of the icon-only control cluster (ambiguous chips/toolbar,
    the target most likely to need pixel disambiguation); else, if a large content
    dead zone exists, the content band itself (un-exposed body). None when the tree is
    informative enough that a full-screen view is fine. Coordinates are screen points.
    Cropping to this before grounding is the biggest accuracy lever (tiny targets are
    the dominant failure mode) and keeps token cost down."""
    icon_ctrls = [
        e for e in space.elements
        if e.type in _CONTROL_TYPES and _uninformative(e.label)
    ]
    if len(icon_ctrls) >= 2:
        return _bbox([e.frame for e in icon_ctrls])
    if space.screen_w > 0 and space.screen_h > 0 and _content_dead_zone(space) >= 0.6:
        w, h = space.screen_w, space.screen_h
        return (0.0, h * _TOP_CHROME_FRAC, float(w),
                h * (1.0 - _TOP_CHROME_FRAC - _BOTTOM_CHROME_FRAC))
    return None


def crop_region(
    screenshot_png: bytes,
    region: tuple[float, float, float, float],
    screen_w: int,
    pad_frac: float = 0.12,
) -> bytes:
    """Crop the screenshot to `region` (in screen points) with padding, so a grounder
    sees a zoomed, high-detail view of the ambiguous area instead of the full retina
    frame. Scales screen points → image pixels by the width ratio. Returns PNG bytes;
    on any failure returns the original bytes (fail-open — never blocks the loop)."""
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(screenshot_png)).convert("RGB")
        scale = img.width / screen_w if screen_w else 1.0
        x, y, w, h = region
        pad_x, pad_y = w * pad_frac, h * pad_frac
        left = max(0, int((x - pad_x) * scale))
        top = max(0, int((y - pad_y) * scale))
        right = min(img.width, int((x + w + pad_x) * scale))
        bottom = min(img.height, int((y + h + pad_y) * scale))
        if right <= left or bottom <= top:
            return screenshot_png
        buf = io.BytesIO()
        img.crop((left, top, right, bottom)).save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return screenshot_png


def _grid(draw, w: int, h: int) -> None:  # noqa: ANN001
    """A faint 10% grid labelled in screen fractions (0.1 … 0.9), so a coordinate tap is READ
    off the image instead of estimated from a guessed pixel size (the model computed
    y as 665/1532 on a 2622 px screenshot, and once y=1.98)."""
    colour = (0, 160, 255)
    for i in range(1, 10):
        x, y = w * i / 10, h * i / 10
        draw.line([(x, 0), (x, h)], fill=colour, width=1)
        draw.line([(0, y), (w, y)], fill=colour, width=1)
        draw.text((x + 2, 2), f".{i}", fill=colour)
        draw.text((2, y + 2), f".{i}", fill=colour)


def set_of_marks(screenshot_png: bytes, space: ActionSpace, out_path: str | Path) -> str | None:
    """Draw a numbered box per element onto the screenshot. Screenshots are in
    device points scaled to pixels; we scale frames by the width ratio."""
    from PIL import Image, ImageDraw

    img = Image.open(io.BytesIO(screenshot_png)).convert("RGB")
    draw = ImageDraw.Draw(img)
    scale = img.width / space.screen_w if space.screen_w else 1.0
    _grid(draw, img.width, img.height)

    for e in space.elements:
        f = e.frame
        box = [f.x * scale, f.y * scale, (f.x + f.width) * scale, (f.y + f.height) * scale]
        draw.rectangle(box, outline=(255, 0, 0), width=2)
        draw.text((box[0] + 2, box[1] + 2), e.ref, fill=(255, 0, 0))

    out = Path(out_path)
    img.save(out)
    return str(out)
