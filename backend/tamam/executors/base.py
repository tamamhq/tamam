"""Executor contract: the swap seam between the harness and any execution engine.

An executor runs a test and returns an evidence bundle. It never decides the
verdict — verification belongs to the harness (checks engine + judge).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated, Any, Awaitable, Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class AppTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")   # a misspelt or old field fails loudly
    """A native app under test (iOS today; Android uses the same shape)."""

    kind: Literal["app"] = "app"
    build_ref: str | None = None       # path or URL of the .app / .ipa, or a farm's app reference
    build_id: str | None = None        # the Build it came from (vendor-upload bookkeeping)
    bundle_id: str | None = None
    device: str | None = None          # e.g. "iPhone 17 Pro"
    os_version: str | None = None      # a farm's OS version, e.g. "26"; None → the farm's default
    backend: str | None = None         # "simulator" | "device" | "cloud"; None → settings
    reuse_session: bool = False        # True → keep app state (a login) across runs


class WebTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")   # a misspelt or old field fails loudly
    """A website under test (the web plugin)."""

    kind: Literal["web"] = "web"
    start_url: str = ""


class ExecutionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")   # a misspelt or old field fails loudly
    instructions: str
    target: Annotated[AppTarget | WebTarget, Field(discriminator="kind")] = Field(
        default_factory=AppTarget)
    platform: str = "ios"               # the executor's registered platform ("ios", "web", …)
    max_steps: int = 25
    max_wall_clock_s: int = 300
    capabilities: list[str] = Field(default_factory=list)
    # Test graduation: the compiled script to replay (executor "script"), its status and id.
    script: dict | None = None
    script_status: str | None = None    # "probation" | "active"
    script_id: str | None = None

    @property
    def app(self) -> AppTarget:
        """The app under test, for app executors (an app executor given a website is a bug)."""
        if not isinstance(self.target, AppTarget):
            raise TypeError(f"an app executor was given a {self.target.kind} target")
        return self.target


class StepEvent(BaseModel):
    step: int
    url: str | None = None
    title: str | None = None
    memory: str | None = None
    next_goal: str | None = None
    actions: list[dict] = Field(default_factory=list)
    # A signature of the screen this step acted on. Equal signatures on consecutive
    # steps mean the prior action had no effect — the no-progress guardrail reads this.
    screen_signature: str | None = None
    # A stable fingerprint of the action taken (type + target, robust to ref churn) —
    # the repeated-action guardrail trips when the same one repeats too many times.
    action_fingerprint: str | None = None
    screenshot_path: str | None = None
    # The set-of-marks annotated image ACTUALLY sent to the model (element-number
    # overlays) — persisted for troubleshooting "did it target the right element".
    annotated_screenshot_path: str | None = None
    duration_s: float | None = None


class SelfVerdict(BaseModel):
    """The executor's own opinion about the outcome. A signal, never the verdict."""

    passed: bool
    reason: str = ""
    reasoning: str = ""
    repro_steps: list[str] = Field(default_factory=list)
    # The agent could not perform a step (a harness/agent limit, not an app fault): the run
    # is inconclusive, never FAILED. One of brain.BlockedCode.
    blocked: str | None = None


class Usage(BaseModel):
    total_tokens: int | None = None
    total_cost: float | None = None
    duration_s: float | None = None


class Diagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid")   # a misspelt or old field fails loudly
    """What went wrong underneath the UI during the run, per platform."""

    errors: list[str] = Field(default_factory=list)          # web console / app device-log errors
    crashes: list[str] = Field(default_factory=list)         # app crashes
    failed_requests: list[dict] = Field(default_factory=list)  # web: 4xx/5xx responses


class ExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")   # a misspelt or old field fails loudly
    steps: list[StepEvent] = Field(default_factory=list)
    # The text of the settled end-state screen (a page's text, or an app's visible labels),
    # captured AFTER the loop together with final_screenshot_path, so checks and the judge
    # grade a stable frame, not a mid-transition one.
    final_text: str | None = None
    final_screenshot_path: str | None = None
    final_url: str | None = None   # web only
    diagnostics: Diagnostics = Field(default_factory=Diagnostics)
    self_verdict: SelfVerdict | None = None
    video_path: str | None = None
    usage: Usage = Field(default_factory=Usage)
    usage_extra: dict = Field(default_factory=dict)  # harness-added (e.g. judge usage)
    error: str | None = None  # infra error — distinct from a failing test
    # The harness's clause checklist and the agent's cited evidence per clause (iOS).
    clauses: list[dict] = Field(default_factory=list)


# (build_id, vendor, app_ref): an app copy was uploaded to a device farm.
UploadRecorder = Callable[[str, str, str], Awaitable[None]]


@dataclass
class RunServices:
    """What the host offers an executor for one run; every part is optional (the CLI and the
    evals offer none). `live` streams the screen to a viewer; `record_upload` records an app
    copy uploaded to a device farm, so deleting the build deletes that copy too."""

    live: Any = None
    record_upload: UploadRecorder | None = None


async def record_upload(services: RunServices | None, build_id: str | None, vendor: str,
                        app_ref: str) -> None:
    """Best effort: bookkeeping must never break a run."""
    if services is None or services.record_upload is None or not build_id or not app_ref:
        return
    try:
        await services.record_upload(build_id, vendor, app_ref)
    except Exception:
        logging.getLogger(__name__).warning("could not record the %s upload of build %s",
                                            vendor, build_id, exc_info=True)


OnEvent = Callable[[StepEvent], Awaitable[None]]
# A liveness ping the executor emits between the slow calls WITHIN a step (observe,
# screenshot, act). It resets the stuck-watchdog's idle clock so a slow-but-live step
# (a heavy screen where each driver call legitimately takes many seconds) isn't mistaken
# for a wedged run — only a call that returns NOTHING for the whole budget trips it.
# Optional: a plain executor can ignore it; the harness supplies a no-op by default.
Heartbeat = Callable[[], Awaitable[None]]


async def _noop_heartbeat() -> None:
    return None


class Executor(Protocol):
    executor_id: str

    async def execute(
        self, spec: ExecutionSpec, on_event: OnEvent, heartbeat: Heartbeat = _noop_heartbeat
    ) -> ExecutionResult: ...
