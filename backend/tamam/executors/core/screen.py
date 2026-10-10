"""How a locked check reads the screen — one definition, shared by the compiler (grounding a
check on the evidence screen) and replay (evaluating it), so they can never disagree."""

from __future__ import annotations

import re


def _bounded(value: str) -> re.Pattern:
    """`value` as a whole token: "$5" must not match "$50", "Books" must not match "Bookshelf"."""
    v = re.escape(value.strip())
    left = r"(?<![\w$€£])" if value[:1].isalnum() or value[:1] in "$€£" else ""
    right = r"(?![\w.,]\d|\w)" if value[-1:].isalnum() or value[-1:] == "%" else ""
    return re.compile(left + v + right, re.IGNORECASE)


def element_shows(text: str, value: str, with_text: str | None = None) -> bool:
    """One element's text shows `value` (whole token) — and `with_text` too, when given, so a
    price is checked on the row it belongs to ("Golden Ticket — $5"), not on any row."""
    if not text or not value or not _bounded(value).search(text):
        return False
    return with_text is None or bool(_bounded(with_text).search(text))


def screen_shows(texts: list[str], value: str, with_text: str | None = None) -> bool:
    return any(element_shows(t, value, with_text) for t in texts or [])


SCREEN_TEXTS_MAX = 150


def screen_texts(elements) -> tuple[list[str], bool]:  # noqa: ANN001
    """The texts a screen shows, as compile-time grounding and replay both read them:
    element labels, top to bottom, capped. Returns (texts, truncated) — a truncated list
    cannot prove something is ABSENT."""
    labels = [e.label for e in elements if getattr(e, "label", "")]
    return labels[:SCREEN_TEXTS_MAX], len(labels) > SCREEN_TEXTS_MAX
