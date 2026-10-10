"""A run's lifecycle and outcome — the trust layer's own vocabulary (the database stores them)."""

from __future__ import annotations

import enum


class Lifecycle(str, enum.Enum):
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    JUDGING = "judging"
    FINISHED = "finished"


class Outcome(str, enum.Enum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
