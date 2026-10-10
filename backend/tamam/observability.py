"""Observability — Tier 1: structured telemetry, or 65% of failures stay invisible.

ONE tool: Laminar. One trace per run, everything nested in a single waterfall:

    run_session
      ├─ harness.execute        ← browser-use auto-instrumented steps land here
      │    └─ per-step traces with screenshots + session replay
      └─ harness.verify
           └─ judge LLM call    ← pydantic-ai spans via Agent.instrument_all()

No lock-in: Laminar is open-source and self-hostable, and everything underneath
is plain OpenTelemetry — the span() seam below is the only integration point, so
swapping backends is a one-file change. Without TAMAM_LMNR_PROJECT_API_KEY set,
every span is a no-op (zero overhead, zero coupling).

Install: uv sync --extra observability
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator

logger = logging.getLogger(__name__)

_laminar: Any = None
_initialized = False


def init_observability(service_name: str) -> None:
    """Idempotent; call once from API lifespan and worker startup.

    Must run BEFORE the executor imports browser_use so Laminar's
    auto-instrumentation can patch it (executor imports are lazy — safe).
    """
    global _laminar, _initialized
    if _initialized:
        return
    _initialized = True

    from tamam.config import get_settings

    settings = get_settings()
    if not settings.lmnr_project_api_key:
        logger.debug("Observability off — set TAMAM_LMNR_PROJECT_API_KEY to enable Laminar")
        return

    try:
        from lmnr import Instruments, Laminar
    except ImportError:
        logger.warning("TAMAM_LMNR_PROJECT_API_KEY set but lmnr not installed "
                       "(uv sync --extra observability) — observability off")
        return

    # lmnr reads this when it records an Anthropic call's prompt and completion.
    import os
    os.environ["LMNR_TRACE_CONTENT"] = "true" if settings.lmnr_trace_content else "false"
    Laminar.initialize(
        project_api_key=settings.lmnr_project_api_key,
        # gRPC is fork-unsafe and browser-use forks subprocesses (Chromium, ffmpeg);
        # the gRPC exporter deadlocked an entire run for 60s until the stuck
        # guardrail killed it. HTTP transport sidesteps the fork hazard.
        force_http=True,
        # Observability must fail-open: if Laminar is unreachable, drop spans
        # fast — never stall the run being observed.
        export_timeout_seconds=5,
        # Explicit instruments. Laminar's auto-detection sees pydantic-ai installed
        # and skips ANTHROPIC to avoid double-instrumentation — but browser-use
        # calls the Anthropic SDK directly, so that skip produced empty traces.
        # SDK-level ANTHROPIC covers BOTH agents (executor inner loop + judge,
        # since pydantic-ai sits on the same SDK), with no doubles because we do
        # NOT also enable pydantic-ai-level instrumentation.
        instruments={
            Instruments.ANTHROPIC,
            Instruments.BROWSER_USE,
            Instruments.BROWSER_USE_SESSION,  # step screenshots / session replay
        },
    )
    _laminar = Laminar

    # lmnr's ANTHROPIC instrumentation copies SDK sentinel values (Omit) onto
    # spans; OTel drops them with a WARNING per attribute per call. Cosmetic —
    # the attribute genuinely has no value — but 3 log lines per LLM call.
    logging.getLogger("opentelemetry.attributes").setLevel(logging.ERROR)

    logger.info("Observability: Laminar (http, fail-open) — browser replay + LLM spans")


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[None]:
    """A traced phase. No-op when Laminar is not configured."""
    if _laminar is None:
        yield
        return
    # Create the span CM first; the TypeError fallback guards ONLY the constructor
    # (older lmnr without the `input` kwarg). It must NOT wrap the `yield`: if it did,
    # a TypeError raised by the body would be caught here and we'd yield a second time,
    # which breaks the @contextmanager contract ("generator didn't stop after throw()")
    # AND masks the real error.
    try:
        cm = _laminar.start_as_current_span(name, input=attributes)
    except TypeError:
        cm = _laminar.start_as_current_span(name)
    with cm:
        yield


def span_output(value: Any) -> None:
    """Attach `value` as the output of the current span (e.g. a device step's trace: the
    commands it sent, their timings, what the agent decided). No-op when Laminar is off;
    fail-open, like every other observability call."""
    if _laminar is None:
        return
    try:
        _laminar.set_span_output(value)
    except Exception:
        logger.debug("could not set span output", exc_info=True)
