"""Typed-text redaction — one policy, applied at the executor's output boundary.

What the agent types can be a secret. The rule:
  • text typed into a SECURE field is a secret;
  • text the test itself does not contain is a secret (an invented email, an OTP);
  • text the test spells out, typed into an ordinary field (a search term), is not — it
    is part of the test, and hiding it would also mangle the screen ("Wireless Mouse")
    that evidence and graduation anchors are read from.
Secrets are replaced by a marker in everything the run emits or stores: step events,
step traces, log lines, agent-view files, clause evidence, the verdict. The model still
sees them (it needs them to type); provider-side LLM traces are a known gap until test
secrets become parameters filled in at the device.
"""

from __future__ import annotations

import re
from typing import Any

MARK = "[typed text]"
_MIN = 3  # shorter values are not secrets, and replacing them would mangle ordinary text

# A credential written into the test ("password hunter2", "PIN: 4821", "token=abc123").
# The agent may repeat it in its notes BEFORE typing it, so these are secrets from step 1.
_CREDENTIAL = re.compile(
    r"\b(?:password|passcode|passwd|pwd|pin|otp|one[- ]time code|token|api key|secret)\b"
    r"\s*(is|:|=)?\s*([\"'“])?([^\s\"'”]{3,})", re.IGNORECASE)


def credentials_in(test_text: str) -> set[str]:
    """Values written next to a credential word in the test. A value counts when it looks like
    a secret (a digit or a symbol in it), or when the test marks it as the value — after ':' or
    '=', or in quotes ("password: sunshine", "PIN 'tiger'"). A bare word does not: "the
    password field" names a field, and scrubbing "field" everywhere would mangle every record."""
    out: set[str] = set()
    for m in _CREDENTIAL.finditer(test_text or ""):
        sep, quote, v = m.group(1), m.group(2), m.group(3).rstrip(".,;:)")
        marked = bool(quote) or (sep or "").strip() in (":", "=")
        if len(v) >= _MIN and (marked or any(c.isdigit() for c in v) or any(not c.isalnum() for c in v)):
            out.add(v)
    return out


class Redactor:
    def __init__(self, test_text: str) -> None:
        self._test_text = test_text or ""
        self._secrets: set[str] = credentials_in(self._test_text)

    def note_typed(self, text: str, *, secure: bool) -> dict:
        """Record a type step; return what may be stored about it (never the text)."""
        in_test = bool(text) and text in self._test_text
        if text and (secure or not in_test):
            self._secrets.add(text)
        return {"chars": len(text or ""), "secure": secure, "in_test_text": in_test}

    @property
    def active(self) -> bool:
        return any(len(s) >= _MIN for s in self._secrets)

    def scrub(self, value: Any) -> Any:
        """`value` with every secret replaced by MARK, recursively (str/dict/list/tuple)."""
        secrets = sorted((s for s in self._secrets if len(s) >= _MIN), key=len, reverse=True)
        if not secrets:
            return value

        def walk(v: Any) -> Any:
            if isinstance(v, str):
                for s in secrets:
                    if s in v:
                        v = v.replace(s, MARK)
                return v
            if isinstance(v, dict):
                return {k: walk(x) for k, x in v.items()}
            if isinstance(v, (list, tuple)):
                return type(v)(walk(x) for x in v)
            return v

        return walk(value)

    def scrub_model(self, model: Any) -> Any:
        """A pydantic model with its fields scrubbed (same type), or None for None."""
        if model is None or not self.active:
            return model
        return type(model).model_validate(self.scrub(model.model_dump()))
