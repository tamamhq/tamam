"""Grounder implementations + the merge that folds vision-found targets into the
action space.

The default `NullGrounder` returns nothing, so wiring the seam is a no-op until a real
grounding model is plugged in (and validated on a grounding eval). When a
grounder DOES return targets, `merge_grounded` appends them to the space as ordinary
tappable elements with fresh refs, so the brain references them exactly like a11y
elements — coordinate grounding is identical (each carries a screen-point frame).
"""

from __future__ import annotations

import dataclasses

import asyncio
import io
import logging

from tamam.executors.core.contracts import ActionSpace, Element, Frame, GroundedElement
from tamam.executors.ios.perceiver import serialize
from tamam.plugins import Registry

logger = logging.getLogger(__name__)


class NullGrounder:
    """No-op grounder — a11y-only behavior. The default until a model is proven."""

    async def detect(
        self,
        screenshot_png: bytes,
        region: tuple[float, float, float, float] | None,
        space: ActionSpace,
    ) -> list[GroundedElement]:
        return []


class OCRGrounder:
    """Deterministic OCR grounder (Tesseract via pytesseract) — NO model/LLM. Finds visible
    TEXT that the accessibility tree omits (text in images / canvas / custom or React-Native
    views — e.g. a populated rich-text editor) and returns it with PRECISE screen
    coordinates, so the brain taps it by ref like any other element instead of guessing.

    This mirrors how browser-use works — the model picks WHICH target; the framework computes
    WHERE from a real bounding box — sourced from pixels here instead of a DOM. Runs only when
    the executor escalates to vision (a11y-weak / stuck steps), so it's off the happy path.

    `_min_conf` drops low-certainty OCR; detections whose text already matches an a11y
    element's label are skipped (the tree wins), and merge_grounded's IoU check is a second
    guard against duplicating something the tree already exposes."""

    def __init__(self, min_conf: int = 45, max_targets: int = 24) -> None:
        self._min_conf = min_conf
        self._max_targets = max_targets

    async def detect(
        self,
        screenshot_png: bytes,
        region: tuple[float, float, float, float] | None,
        space: ActionSpace,
    ) -> list[GroundedElement]:
        # OCR is CPU-bound → run off the event loop.
        return await asyncio.to_thread(self._detect_sync, screenshot_png, space)

    def _detect_sync(self, png: bytes, space: ActionSpace) -> list[GroundedElement]:
        try:
            import pytesseract
            from PIL import Image
        except Exception:
            logger.warning("OCR grounder needs pytesseract + Pillow + the tesseract binary")
            return []
        try:
            img = Image.open(io.BytesIO(png))
            img_w = img.width or 1
            # Screenshot is retina (pixels); Element frames are in POINTS. Convert with the
            # width ratio (same convention as crop_region).
            scale = img_w / space.screen_w if space.screen_w else 1.0
            data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
        except Exception:
            logger.warning("OCR failed; leaving the a11y space unchanged", exc_info=True)
            return []

        # Group Tesseract words into LINES (block/par/line key), union their boxes, average
        # confidence — a line like "Edit target alpha" becomes one tappable target.
        lines: dict[tuple, dict] = {}
        n = len(data.get("text", []))
        for i in range(n):
            text = (data["text"][i] or "").strip()
            try:
                conf = float(data["conf"][i])
            except (ValueError, TypeError):
                conf = -1.0
            if not text or conf < self._min_conf:
                continue
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
            ln = lines.setdefault(key, {"words": [], "x0": x, "y0": y, "x1": x + w,
                                        "y1": y + h, "confs": []})
            ln["words"].append(text)
            ln["x0"], ln["y0"] = min(ln["x0"], x), min(ln["y0"], y)
            ln["x1"], ln["y1"] = max(ln["x1"], x + w), max(ln["y1"], y + h)
            ln["confs"].append(conf)

        known = {(e.label or "").strip().lower() for e in space.elements}
        # Scope OCR to the content the a11y tree is actually blind to. Exclude two app-
        # agnostic noise bands: the top status bar (clock/battery) and — when a keyboard is
        # up — the on-screen keyboard, whose keys OCR reads as garbled letter-rows. This is
        # what makes OCR ADD the invisible field text without flooding the action space.
        h = space.screen_h or 1
        top_chrome_y = 0.055 * h
        kb_up = any((e.label or "").strip().lower() in ("return", "space", "shift",
                                                        "hide keyboard", "delete", "emoji", "dictate")
                    for e in space.elements)
        # The keyboard's real top when the tree gave it (the 55% guess cut off a form's
        # "Password" label above the keyboard); the rough guess otherwise.
        if getattr(space, "keyboard", False) and getattr(space, "keyboard_top", 0):
            kb_top_y = space.keyboard_top
        else:
            kb_top_y = 0.55 * h if kb_up else h  # keyboard occupies roughly the bottom ~45%

        out: list[GroundedElement] = []
        for ln in lines.values():
            text = " ".join(ln["words"]).strip()
            low = text.lower()
            if len(text) < 3 or low in known:  # too short / already a known a11y element
                continue
            # Require a real word (a run of ≥3 letters) — drops keyboard letter-soup and
            # symbol clusters like ">B I!" / "QW ER O P" that OCR emits over the keyboard.
            if not any(len(w) >= 3 and w.isalpha() for w in ln["words"]):
                continue
            px, py = ln["x0"], ln["y0"]
            pw, ph = ln["x1"] - ln["x0"], ln["y1"] - ln["y0"]
            frame = Frame(x=px / scale, y=py / scale, width=pw / scale, height=ph / scale)
            cy = frame.y + frame.height / 2.0
            if cy < top_chrome_y or cy > kb_top_y:  # status bar / keyboard → noise, skip
                continue
            conf = sum(ln["confs"]) / len(ln["confs"]) / 100.0
            out.append(GroundedElement(label=text, frame=frame, confidence=conf))

        # Bound how many we add (keep the most confident) so a text-dense screen can't flood
        # the action space.
        out.sort(key=lambda g: g.confidence, reverse=True)
        return out[: self._max_targets]


def ocr_lines(png_path: str | None, min_conf: int = 45) -> list[str]:
    """The text lines Tesseract reads in a screenshot (deterministic, no model), or [] when the
    image, the package or the binary is missing. Used to VERIFY quoted evidence the
    accessibility tree cannot show (a system sheet drawn outside the app, a canvas)."""
    if not png_path:
        return []
    try:
        import pytesseract
        from PIL import Image
        data = pytesseract.image_to_data(Image.open(png_path), output_type=pytesseract.Output.DICT)
    except Exception:
        return []
    lines: dict[tuple, list[str]] = {}
    for i, raw in enumerate(data.get("text", [])):
        text = (raw or "").strip()
        try:
            conf = float(data["conf"][i])
        except (ValueError, TypeError):
            conf = -1.0
        if text and conf >= min_conf:
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            lines.setdefault(key, []).append(text)
    return [" ".join(w) for w in lines.values()]


GROUNDERS: Registry = Registry("grounders")
GROUNDERS.register("ocr", lambda: OCRGrounder())
GROUNDERS.register("null", lambda: NullGrounder())


def build_grounder(kind: str):
    """The grounder named in settings (`ios_grounder`), from the registry — a plugin can add
    its own. An unknown name falls back to accessibility-only, with a warning."""
    if kind not in GROUNDERS:
        logger.warning("unknown grounder %r; using none (registered: %s)", kind, GROUNDERS.names())
        return NullGrounder()
    return GROUNDERS.create(kind)


def _overlaps_existing(g: GroundedElement, space: ActionSpace, iou_min: float = 0.6) -> bool:
    """Skip a detection that lands on an element the a11y tree already exposes — the
    tree's label/identity is better; vision only fills gaps, it doesn't duplicate."""
    gf = g.frame
    ga = gf.width * gf.height
    if ga <= 0:
        return True
    for e in space.elements:
        ef = e.frame
        ix = max(0.0, min(gf.x + gf.width, ef.x + ef.width) - max(gf.x, ef.x))
        iy = max(0.0, min(gf.y + gf.height, ef.y + ef.height) - max(gf.y, ef.y))
        inter = ix * iy
        union = ga + ef.width * ef.height - inter
        if union > 0 and inter / union >= iou_min:
            return True
    return False


def merge_grounded(space: ActionSpace, grounded: list[GroundedElement]) -> ActionSpace:
    """Return a new ActionSpace with vision-found targets appended as tappable Elements.
    Fresh refs continue the e-numbering so the brain names them like any other element;
    detections overlapping an existing a11y element are dropped (the tree wins). If no
    detections survive, the original space is returned unchanged."""
    fresh = [g for g in grounded if not _overlaps_existing(g, space)]
    if not fresh:
        return space
    start = len(space.elements)
    added = [
        Element(
            ref=f"e{start + i + 1}",
            type="Image",  # vision targets are icon-like; typed as Image, still tappable
            label=g.label or "(vision target)",
            frame=g.frame,
        )
        for i, g in enumerate(fresh)
    ]
    elements = list(space.elements) + added
    extra = serialize(added)
    text = f"{space.text}\n{extra}" if space.text else extra
    # A copy with the added targets — everything else the read carried (the keyboard flag, an
    # app dialog's title, harness notes, the step) stays. Rebuilding it dropped them all: with
    # OCR on, a screen with a keyboard read as keyboard-free, so the under-keyboard tap guard,
    # the keyboard hook and the retap never ran.
    return dataclasses.replace(space, elements=elements, text=text)


INPUT_SCAN_PT = 110      # how far below a label to look for its input box
INPUT_MIN_PT = 20        # a box shorter than this is a divider, not a field
INPUT_MAX_PT = 90
_EDGE_DIFF = 18          # luminance difference from the background that marks a border
_EDGE_FRAC = 0.8         # share of a row that must differ for it to be a border line


def input_box_below(png_path: str | None, label: Frame, screen_w: float) -> Frame | None:
    """The input box drawn under a field label, found in the pixels: forms put the box right
    below its label, and a box shows as border lines across the content width. For forms
    whose fields are hidden from the accessibility tree (a merged / flattened view) — the
    agent sees the label (tree or OCR) but has no element to type into. Returns the
    box in screen points, or None. Two borders = a box; one line = an underline field."""
    if not png_path:
        return None
    try:
        from PIL import Image
        im = Image.open(png_path).convert("L")
    except Exception:
        return None
    w, h = im.size
    k = w / screen_w if screen_w else 1.0
    px = im.load()
    xa, xb = int(16 * k) + 4, int((screen_w - 16) * k) - 4
    xs = range(xa, max(xa + 1, xb), 3)
    y_start = int((label.y + label.height) * k) + 2
    if y_start >= h:
        return None
    bg = sorted(px[x, y_start] for x in xs)[len(xs) // 2]

    def is_edge(y: int) -> bool:
        return sum(1 for x in xs if abs(px[x, y] - bg) > _EDGE_DIFF) >= _EDGE_FRAC * len(xs)

    edges: list[int] = []
    y, end = y_start, min(h, y_start + int(INPUT_SCAN_PT * k))
    while y < end and len(edges) < 2:
        if is_edge(y):
            edges.append(y)
            while y < end and is_edge(y):
                y += 1                       # skip the rest of this (thick) border
            if len(edges) == 2 and (edges[1] - edges[0]) / k < INPUT_MIN_PT:
                edges.clear()                # two lines too close: a bar or divider, not a field
        y += 1
    if len(edges) == 2 and (edges[1] - edges[0]) / k <= INPUT_MAX_PT:
        top, bottom = edges[0] / k, edges[1] / k
        return Frame(xa / k, top, (xb - xa) / k, bottom - top)
    if len(edges) == 1:                       # an underline: the field sits just above it
        line = edges[0] / k
        return Frame(xa / k, line - 30, (xb - xa) / k, 30)
    return None
