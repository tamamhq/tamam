"""Executor registry: which execution engines exist, and what each one is.

Adding an executor = one factory + one `register()` call (or an entry point in the
`tamam.executors` group, see tamam.plugins). The session, verification pipeline and UI all
speak ExecutionSpec/ExecutionResult, and read only the metadata registered here:

- platform:  what it tests ("ios", "web", …); the harness passes it on in the spec.
- graduates: a passing run of it can be compiled into a fast replay.
- internal:  chosen by the harness itself (replay), never offered to users.
- default:   preferred for tests that name no executor (at most one should claim it).
"""

from __future__ import annotations

from typing import Callable

from tamam.executors.base import Executor
from tamam.plugins import Registry

EXECUTORS: Registry[Executor] = Registry("executors")
FALLBACK = "ios_agent"


def register(executor_id: str, factory: Callable[[], Executor], *, platform: str,
             graduates: bool = False, internal: bool = False, default: bool = False,
             replace: bool = False) -> None:
    EXECUTORS.register(executor_id, factory, replace=replace, platform=platform,
                       graduates=graduates, internal=internal, default=default)


def get_executor(executor_id: str) -> Executor:
    return EXECUTORS.create(executor_id)


def registered_executors() -> list[str]:
    return EXECUTORS.names()


def user_executors() -> list[str]:
    """Executors a test may name (replay is the harness's own choice)."""
    return [e for e in EXECUTORS.names() if not EXECUTORS.info(e, "internal", False)]


def platform_of(executor_id: str) -> str:
    return EXECUTORS.info(executor_id, "platform", "ios")


def graduates(executor_id: str | None) -> bool:
    return bool(executor_id) and executor_id in EXECUTORS and EXECUTORS.info(executor_id, "graduates", False)


def default_executor() -> str:
    preferred = [e for e in EXECUTORS.names() if EXECUTORS.info(e, "default", False)]
    return preferred[0] if preferred else FALLBACK


# ── built-ins (factories import lazily, so a part's dependencies load only when used) ─────

def _ios_agent() -> Executor:
    from tamam.executors.ios.executor import IOSAgentExecutor

    return IOSAgentExecutor()


def _script() -> Executor:
    from tamam.executors.ios.replay_device import IOSReplayPlatform
    from tamam.graduation.executor import ScriptExecutor

    return ScriptExecutor(IOSReplayPlatform())


register("ios_agent", _ios_agent, platform="ios", graduates=True)
# A replay that passes keeps its script graduated; when it can't replay it hands over to the
# iOS agent inside the same run.
register("script", _script, platform="ios", graduates=True, internal=True)
