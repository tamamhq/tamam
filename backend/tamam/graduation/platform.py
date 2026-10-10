"""What fast replay needs from a platform — the seam that keeps graduation platform-free.

Replay drives a device through a replay session; on a miss the AI agent may take over on the
same device. A platform (iOS today) provides both behind these two small interfaces, and the
executor registry wires one into ScriptExecutor. Graduation never imports a platform package.
"""

from __future__ import annotations

from typing import Any, Protocol

from tamam.executors.base import ExecutionSpec, Executor, RunServices


class OpenDevice(Protocol):
    """A started device session that replay is driving."""

    session: Any            # what ReplayRunner acts through (graduation.replay)
    reconnects_used: int    # a hand-off shares the run's reconnect budget

    async def stop(self) -> str | None:
        """End the app session; returns the screen recording, if any. Never raises."""

    async def release(self) -> None:
        """Give the device back (a farm device keeps billing until released). Never raises."""


class ReplayPlatform(Protocol):
    def attach(self, services: RunServices) -> None: ...

    async def open(self, spec: ExecutionSpec) -> OpenDevice:
        """Provision and start a device for replay; on failure, release it and raise."""

    def agent(self, device: OpenDevice | None = None, **handoff: Any) -> Executor:
        """The AI executor: a fresh one, or one attached to `device` to finish a replay's run
        (handoff: preset_clauses, seed_history)."""
