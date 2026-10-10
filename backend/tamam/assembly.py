"""The composition root: one place that assembles a run from its parts.

The dashboard (runtime/session.py), the evals and the `tamam` CLI all build their runs here,
so they run the same harness: the same guardrails, verifier, judge and planner. Each part comes
from settings or a registry (tamam.plugins), and any of them can be passed in to replace it —
that is how tests inject fakes and how a new part is tried before it becomes a default.
"""

from __future__ import annotations

from typing import Any

from tamam.config import Settings, get_settings
from tamam.executors.base import Executor
from tamam.harness.contracts import EventSink, Judge
from tamam.harness.guardrails import standard_guardrails
from tamam.harness.run import HarnessRun
from tamam.harness.spec import RunSpec
from tamam.harness.verification import Verifier
from tamam.plugins import Registry

JUDGES: Registry[Judge] = Registry("judges")


def _anthropic_judge() -> Judge:
    from tamam.harness.judge import AnthropicJudge

    return AnthropicJudge()


JUDGES.register("anthropic", _anthropic_judge)

FROM_SETTINGS: Any = object()   # "use what the settings say" (None means "none at all")


def build_judge(settings: Settings) -> Judge | None:
    return JUDGES.create(settings.judge_name) if settings.judge_enabled else None


def build_planner(settings: Settings):  # noqa: ANN201
    # Auto-planned checks are off by default: the planner mis-extracts concept words (e.g.
    # "SSO") into literal text_present checks that no real UI shows, causing false FAILs.
    if not settings.auto_plan:
        return None
    from tamam.harness.planner import AutoPlanner

    return AutoPlanner(settings.judge_model)


def assemble_run(spec: RunSpec, executor: Executor, events: EventSink, *,
                 judge: Judge | None = FROM_SETTINGS, planner=FROM_SETTINGS,  # noqa: ANN001
                 settings: Settings | None = None) -> HarnessRun:
    settings = settings or get_settings()
    return HarnessRun(
        spec=spec,
        executor=executor,
        guardrails=standard_guardrails(spec.budgets),
        verifier=Verifier(
            judge=build_judge(settings) if judge is FROM_SETTINGS else judge,
            planner=build_planner(settings) if planner is FROM_SETTINGS else planner,
        ),
        events=events,
    )
