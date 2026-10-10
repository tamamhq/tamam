"""Executor routing — code-before-prompt applied to execution itself.

The cheapest executor that can do the job wins: a test's own choice, then its deterministic
replay, then the default AI executor (see executors/registry.py).
"""

from __future__ import annotations

from tamam.executors.registry import default_executor
from tamam.executors.registry import get_executor as get_executor  # re-export
from tamam.models import Test

SCRIPT = "script"


def select_executor(test: Test) -> str:
    if test.executor_id:
        return test.executor_id  # explicit override
    if test.script:
        return SCRIPT  # legacy inline script: deterministic replay
    return default_executor()


# The deterministic path is test GRADUATION (tamam/graduation/, docs/guide/test-graduation.md):
# a passing AI run is compiled into a script; runtime/session.py routes a run to the "script"
# executor when lifecycle.current_script() says it may replay, with a full AI run as its
# safety net. (`test.script` above is the legacy inline-script field.)
