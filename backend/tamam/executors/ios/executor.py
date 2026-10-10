"""IOSAgentExecutor — our iOS executor, behind the same Executor contract as web.

Same loop as the web AgentExecutor: observe → (vision if needed) → decide → act,
until the brain says done or the step budget is hit. Streams a StepEvent per step
and returns an evidence bundle (steps, final screen text, device/crash logs,
self-verdict, video). Every collaborator is injected, so the loop is unit-testable
with fakes — no device, no Appium, no API key.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import base64
import io
import json
import logging
import time
from pathlib import Path
from typing import Any

from tamam.config import get_settings
from tamam.executors.base import (
    Diagnostics, ExecutionResult, ExecutionSpec, OnEvent, RunServices, SelfVerdict, StepEvent, Usage, _noop_heartbeat,
)
from tamam.executors.ios.brain import (BackgroundApp, Done, PydanticAIDecider, Turn, Type, Wait, render_step)
from tamam.executors.core.redact import Redactor
from tamam.executors.ios.session import DeviceSession, SessionLost
from tamam.executors.core.screen import screen_texts
from tamam.executors.ios.hooks import (Hook, Hooks, SettledVerdictGate, StepContext, default_hooks,
                                     fingerprint)
from tamam.executors.ios.plan import ClauseLedger
from tamam.harness.clauses import HeuristicPlanner, ModelPlanner
from tamam.executors.ios.perceiver import is_opaque_label
from tamam.executors.core.contracts import DeviceDriver, Perceiver, VisionPolicy
from tamam.executors.ios.driver import DriverSessionDead
from tamam.executors.ios.grounder import NullGrounder, build_grounder, merge_grounded
from tamam.executors.ios.perceiver import IOSPerceiver
from tamam.executors.ios.vision import AXFirstVisionPolicy, low_info_region, set_of_marks
from tamam.observability import span, span_output

logger = logging.getLogger(__name__)

LIVE_POLL_S = 0.33  # live-preview screenshot cadence while a viewer is watching
_SETTLE_S = 1.0        # grace for a slow transition before "did not change" is believed
_SETTLE_POLL_S = 0.2


async def _settle(driver: Any) -> None:
    """Let the transition an action started finish — but only as long as it takes: two cheap
    screenshots in a row that match mean the screen is still. Capped at _SETTLE_S, so never
    slower than the old flat pause (a blinking cursor or a spinner simply reaches the cap).
    Remote devices keep the flat pause: a screenshot over the network costs more than it saves
   ."""
    if _SETTLE_S <= 0:
        return
    grab = getattr(driver, "screenshot_bytes", None)
    if grab is None or getattr(driver, "remote", False):
        await asyncio.sleep(_SETTLE_S)
        return
    start = time.monotonic()
    prev = None
    await asyncio.sleep(0.15)
    while time.monotonic() - start < _SETTLE_S:
        try:
            cur = hash(await grab())
        except Exception:
            await asyncio.sleep(max(0.0, _SETTLE_S - (time.monotonic() - start)))
            return
        if cur == prev:
            return
        prev = cur
        await asyncio.sleep(_SETTLE_POLL_S)
_DECIDE_RETRIES = 2   # transient model/API failures retried before the run ERRORs
_DECIDE_BACKOFF_S = 2.0


def _tokens(decider: Any, ledger: Any) -> int | None:
    used = getattr(decider, "total_tokens", None)
    extra = getattr(getattr(ledger, "_planner", None), "total_tokens", 0) or 0
    return used + extra if used is not None else (extra or None)



TRACE_SCHEMA = 2
_SPAN_MAX = 8000  # bytes of one step trace sent to the tracing backend


def _traced(step: dict) -> dict:
    """The finished step trace, also recorded on the current `ios.step` span, so device-side
    evidence survives a deploy that wipes the worker's disk. Capped: a scroll loop's command
    log can be long, and the stored JSONL keeps the full record."""
    out = step
    if len(json.dumps(step, default=str)) > _SPAN_MAX:
        diag = dict(step.get("diag") or {})
        dev = diag.get("device") or []
        slow = sorted(dev, key=lambda c: c.get("s") or 0, reverse=True)[:5]
        diag["device"] = {"commands": len(dev), "slowest": slow}
        out = {**step, "diag": diag}
    span_output(out)
    return step


def _anchors(space: Any, exclude: str = "", n: int = 5) -> list[str]:
    """A few stable texts that identify the screen (top to bottom): no digits, short, not the
    action's own target. What graduation re-checks to know it is on the same screen."""
    out: list[str] = []
    for e in getattr(space, "elements", []):
        label = (e.label or "").strip()
        if (label and label != exclude and len(label) <= 40 and label not in out
                and not any(ch.isdigit() for ch in label) and not is_opaque_label(label)):
            out.append(label)
        if len(out) >= n:
            break
    return out


def _target_of(action: Any, space: Any) -> dict | None:
    """The element an action was aimed at, by what survives the run (type, label, place on
    screen, state) — not its per-screen ref."""
    el = space.by_ref(action.ref) if getattr(action, "ref", None) else None
    if el is None:
        return None
    w, h = (space.screen_w or 1), (space.screen_h or 1)
    f = el.frame
    t = {"type": el.type, "label": el.label, "identifier": el.identifier,
         "frame_frac": [round(f.x / w, 4), round(f.y / h, 4), round(f.width / w, 4), round(f.height / h, 4)]}
    if el.toggled is not None:
        t["toggled"] = el.toggled
    if el.selected:
        t["selected"] = True
    return t


class _StepTrace:
    """One step's record: what the agent aimed at (by stable identity), the screen it was on,
    what it decided, and — under `diag` — where the seconds went (every driver command, timed).
    One versioned JSON line per step; the run's JSONL is stored with the run and is what
    test graduation compiles from. Secrets are redacted before it leaves the executor."""

    def __init__(self, step: int, driver: Any, ctx: Any, hist_len: int) -> None:
        import time as _t
        self._t = _t
        self.t0 = self.last = _t.monotonic()
        self.step, self.driver, self.ctx, self.hist_len = step, driver, ctx, hist_len
        self.phases: dict[str, float] = {}
        self.record: dict = {}
        self.diag: dict = {}
        self.source: str | None = None
        self.agent_view: str | None = None
        self.finished = False
        ctx.hook_times = {}
        log = getattr(driver, "call_log", None)
        self._log_start = len(log) if isinstance(log, list) else 0

    def mark(self, phase: str) -> None:
        now = self._t.monotonic()
        self.phases[phase] = round(self.phases.get(phase, 0) + now - self.last, 2)
        self.last = now

    def observed(self, space: Any) -> None:
        els = getattr(space, "elements", [])
        texts, truncated = screen_texts(els)
        self.record["screen"] = {
            "anchors": _anchors(space), "tree_hash": space.source_hash,
            "size_pt": [space.screen_w, space.screen_h],
            # What the screen said and which switches were on: what a check is grounded on.
            "texts": texts,
            "texts_truncated": truncated,
            "toggles": {e.label: e.toggled for e in els if e.toggled is not None and e.label},
            "selected": [e.label for e in els if e.selected and e.label],
        }
        self.diag.update({"elements": len(space.elements)})

    def decision(self, decision: Any, space: Any, ctx: Any) -> None:
        # Exactly the text the model was sent this step (checklist, history, elements).
        self.agent_view = render_step(space, ctx.history)
        turn = decision if isinstance(decision, Turn) else None
        action = turn.action if turn else decision
        dumped = action.model_dump(exclude_none=True)
        target = _target_of(action, space)
        if isinstance(action, Type):
            # No target (typing into whatever has focus): we cannot tell it is not a
            # password field, so treat it as one.
            secure = target is None or target["type"] == "SecureTextField"
            dumped["typed"] = ctx.redactor.note_typed(action.text, secure=secure)
        self.record.update({
            "action": dumped,
            "target": target,
            "decision": {"evaluation": turn.evaluation_previous if turn else None,
                         "memory": turn.memory if turn else None,
                         "clause_updates": [u.model_dump() for u in turn.clause_updates] if turn else []},
        })
        self.diag["vision"] = ctx.screenshot is not None

    def finish(self, history: list[str], error: str | None = None) -> dict:
        self.finished = True
        log = getattr(self.driver, "call_log", None)
        device = log[self._log_start:] if isinstance(log, list) else []
        out = {
            "schema": TRACE_SCHEMA,
            "step": self.step,
            **self.record,
            "notes": history[self.hist_len:],
            "diag": {"total_s": round(self._t.monotonic() - self.t0, 2), "phases": self.phases,
                     "hooks": dict(self.ctx.hook_times), "device": device, **self.diag},
        }
        if error:
            out["error"] = error
        out = self.ctx.redactor.scrub(out)
        line = json.dumps(out, default=str)
        logger.info("step_trace %s", line[:4000])
        run_dir = getattr(self.driver, "_dir", None)
        if run_dir is not None:
            try:
                d = Path(run_dir)
                with open(d / "step_trace.jsonl", "a") as fh:
                    fh.write(line + "\n")
                if self.source:
                    (d / f"step_{self.step}_source.xml").write_text(self.source)
                if self.agent_view:
                    (d / f"step_{self.step}_agent_view.txt").write_text(
                        self.ctx.redactor.scrub(self.agent_view))
            except Exception:
                pass
        return out





def _friendly_error(exc: Exception) -> str:
    s = str(exc).lower()
    if "could not start a new session" in s or "wda" in s or "webdriveragent" in s:
        return "Could not start the iOS session (Appium/WebDriverAgent)"
    if "app" in s and ("install" in s or "not found" in s):
        return "Could not install or launch the app build"
    if "timeout" in s:
        return "Timed out driving the app"
    return f"iOS executor error: {exc}"


@dataclass
class _Run:
    """One run's moving parts and what it has gathered so far (IOSAgentExecutor.execute)."""

    spec: ExecutionSpec
    on_event: OnEvent
    heartbeat: Any
    perceiver: Any
    decider: Any
    ledger: Any
    ctx: Any
    hooks: Any
    decider_timeout: float
    session: Any = None
    steps: list = field(default_factory=list)
    trace: list = field(default_factory=list)   # per-step timing + what the agent and harness saw
    tr: Any = None                              # the step being traced
    self_verdict: SelfVerdict | None = None
    error: str | None = None


class IOSAgentExecutor:
    executor_id = "ios_agent"

    def __init__(
        self,
        *,
        provider: Any | None = None,
        driver: DeviceDriver | None = None,
        perceiver: Perceiver | None = None,
        vision: VisionPolicy | None = None,
        decider: Any | None = None,
        grounder: Any | None = None,
        hooks: list[Hook] | None = None,
        planner: Any | None = None,
        attached: bool = False,
        preset_clauses: list | None = None,
        seed_history: list[str] | None = None,
        reconnects_used: int = 0,
    ) -> None:
        # Hand-off from a fast replay: the driver is LIVE (already started, app on the screen
        # replay reached) — do not start it again; the checklist arrives pre-filled with what
        # replay proved, and the history says where replay stopped and why.
        self._attached = attached
        self._reconnects_used = reconnects_used   # a hand-off shares the run's reconnect budget
        self._preset_clauses = preset_clauses
        self._seed_history = list(seed_history or [])
        self._provider = provider
        self._driver = driver
        self._perceiver = perceiver
        self._vision = vision
        self._decider = decider
        self._grounder = grounder
        self._extra_hooks = list(hooks or [])  # appended after the default pipeline
        self._planner = planner
        self._services = RunServices()

    def attach(self, services: RunServices) -> None:
        """The host's services for this run: live view, farm-upload bookkeeping."""
        self._services = services

    @property
    def _live(self) -> Any:
        return self._services.live

    async def execute(
        self, spec: ExecutionSpec, on_event: OnEvent, heartbeat=_noop_heartbeat
    ) -> ExecutionResult:
        perceiver = self._perceiver or IOSPerceiver()
        decider = self._decider or PydanticAIDecider(get_settings().agent_model)
        self._reconnects = self._reconnects_used
        self._max_reconnects = get_settings().max_session_reconnects
        ledger = ClauseLedger(self._planner_for(decider), max_steps=spec.max_steps,
                              preset=self._preset_clauses)
        ctx = StepContext(objective=spec.instructions, driver=None,
                          redactor=Redactor(spec.instructions))
        ctx.history.extend(self._seed_history)
        run = _Run(spec=spec, on_event=on_event, heartbeat=heartbeat, perceiver=perceiver,
                   decider=decider, ledger=ledger, ctx=ctx, hooks=self._hooks(ledger),
                   decider_timeout=get_settings().decider_timeout_s)
        driver: Any = None  # built inside the try so the finally always runs cleanup
        governor: asyncio.Task | None = None
        device_errors: list[str] = []
        crashes: list[str] = []
        final_text = ""
        final_shot: str | None = None
        try:
            # Build the driver INSIDE the try: if provisioning fails (e.g. a cloud
            # device/session error), the finally still runs cleanup — otherwise a
            # remote-access session (AWS Device Farm) would stay open and drain quota.
            driver = await self._build_driver(spec)
            ctx.driver = driver
            # One live device, read and driven with the harness's recovery rules (shared with
            # test replay). The reconnect budget stays the executor's: it spans the run.
            run.session = DeviceSession(driver, perceiver, reconnect=lambda: self._try_reconnect(driver))
            if not self._attached:
                await self._start_with_reconnect(driver)
            governor = self._start_live_governor(driver)

            for step_num in range(1, spec.max_steps + 1):
                with span("ios.step", step=step_num):
                    if await self._step(run, step_num):
                        break

            # Settled end-state evidence: the last step's screenshot is captured
            # BEFORE its action lands (mid-transition — spinners, toasts, a network
            # flash). Wait for the screen to stop changing, then capture the final
            # a11y + screenshot TOGETHER so the judge grades a stable frame.
            # The verdict is already decided by here. Everything below is evidence
            # gathering, and on a degraded app it is the most expensive part of the run
            # (up to four full snapshots) — so it must keep signalling liveness, or the
            # stuck-watchdog destroys a verdict the agent already earned.
            final_text, final_shot = await run.session.final(heartbeat)
            await heartbeat()
            await self._safe_capture_logs(driver)
            await heartbeat()
            device_errors, crashes = self._safe_logs(driver)
        except asyncio.CancelledError:
            # A harness guardrail aborted the run (step/wall-clock/stuck/no-progress).
            # Swallow the cancel and fall through so the partial evidence gathered so
            # far — steps + their screenshots, and the video finalized in `finally` —
            # is still RETURNED and persisted. The harness stamps the guardrail error.
            # driver may be None if the cancel landed DURING provisioning (build) — guard it.
            logger.info("IOSAgentExecutor cancelled by guardrail; preserving partial evidence")
            device_errors, crashes = self._safe_logs(driver)
        except Exception as exc:
            logger.exception("IOSAgentExecutor failed")
            run.error = _friendly_error(exc)
        finally:
            if run.tr is not None and not run.tr.finished:
                # The step that died (a timeout, a dead session) is the one most worth keeping.
                run.trace.append(_traced(run.tr.finish(ctx.history, error=run.error or "step did not complete")))
            await _stop_governor(governor)
            video = await _safe_stop(driver) if driver is not None else None
            await self._safe_cleanup()  # stops the cloud/remote session even if build failed
        return self._result(run, driver, final_text, final_shot, device_errors, crashes, video)

    def _hooks(self, ledger: ClauseLedger) -> Hooks:
        """The per-step hook pipeline: the defaults, the clause ledger, the settle gate (last:
        one extra read, only on an accepted PASS), then any injected hooks."""
        vision = self._vision or AXFirstVisionPolicy()
        grounder = self._grounder or build_grounder(get_settings().ios_grounder)
        return Hooks(default_hooks(vision_policy=vision, grounder=grounder,
                                   som=self._safe_som, grounder_fn=self._augment_with_grounder)
                     + [ledger, SettledVerdictGate()] + list(self._extra_hooks))

    async def _step(self, run: "_Run", step_num: int) -> bool:
        """One turn: observe the screen, ask the brain, act. True ends the run."""
        ctx, hooks, heartbeat, history = run.ctx, run.hooks, run.heartbeat, run.ctx.history
        driver = ctx.driver
        ctx.step_num = step_num
        ctx.screenshot = ctx.annotated_path = None
        tr = run.tr = _StepTrace(step_num, driver, ctx, len(history))
        await hooks.before_observe(ctx)
        tr.mark("before_observe_hooks")
        if ctx.action_note_idx is not None and not isinstance(ctx.last_action, (Wait, BackgroundApp)):
            # Let the transition an action started finish BEFORE the one read of this step — a
            # read taken mid-flight reports "did not change" and the agent retries (a double
            # tap). A pause is free; a second tree read is not (each full read walks — and on
            # list screens, pages in — the whole app).
            await _settle(driver)
            tr.mark("settle_pause")
        await heartbeat()  # device prep done — alive before the (slow) observe
        space = await run.session.observe(heartbeat)
        tr.mark("observe")
        tr.source = getattr(run.perceiver, "last_source", None)
        await heartbeat()  # slow observe on a heavy screen is progress, not a stall
        if space is None:
            # The device stayed unresponsive through a recovery attempt — end the loop cleanly.
            # The partial evidence gathered so far is still returned and JUDGED (not an infra
            # error): a run that did the task before the device wedged still gets a real
            # verdict, and a repeatedly-crashing app is itself a finding.
            logger.warning("device unresponsive after recovery; ending with partial evidence")
            return True
        space.step = step_num
        ctx.space = space
        ctx.session_busy = False  # an observation got through — the session is free
        ctx.shot_path = await run.session.screenshot()
        tr.mark("screenshot")
        await hooks.after_observe(ctx)
        tr.mark("after_observe_hooks")
        space = ctx.space
        tr.observed(space)
        await heartbeat()  # screenshot/vision done — still alive before the model call

        try:
            decision = await self._decide_with_retry(
                run.decider, run.spec.instructions, space, history, ctx.screenshot,
                run.decider_timeout, heartbeat)
        except (asyncio.TimeoutError, TimeoutError):
            # A wedged decision-model call is OUR infra failing, not the app. Judging the
            # partial evidence here would risk a false FAIL of a fine app that simply wasn't
            # finished driving; per the harness invariant, our-infra failures are ERROR
            # (inconclusive), never a FAIL.
            logger.warning("decider timed out after %ss; ending as infra ERROR", run.decider_timeout)
            run.error = ("The decision model did not respond in time (infrastructure "
                         "timeout); the run is inconclusive.")
            return True
        tr.mark("decide")
        tr.decision(decision, space, ctx)
        action, memory = self._unpack(decision, ctx, run.ledger, history, step_num)
        fp = fingerprint(action, space)

        event = StepEvent(
            step=step_num,
            url=None,
            next_goal=ctx.redactor.scrub(getattr(action, "reason", "") or action.action),
            memory=ctx.redactor.scrub(memory),
            actions=[ctx.redactor.scrub(action.model_dump())],
            screen_signature=space.source_hash,  # NoProgress guardrail reads this
            action_fingerprint=fp,               # RepeatedAction guardrail reads this
            screenshot_path=ctx.shot_path,
            annotated_screenshot_path=ctx.annotated_path,
        )
        run.steps.append(event)
        await run.on_event(event)

        if isinstance(action, Done):
            # Stop gates (hooks) may refuse a verdict that the evidence does not support
            # yet; the refusal becomes the next turn's context.
            refusal = await hooks.on_done(ctx, action)
            if refusal:
                history.append(refusal)
                run.trace.append(_traced(tr.finish(history)))
                return False
            run.self_verdict = SelfVerdict(
                passed=action.passed, reason=action.reason,
                reasoning=action.reasoning, repro_steps=action.repro_steps,
                blocked=None if action.passed else action.blocked,
            )
            run.trace.append(_traced(tr.finish(history)))
            return True

        ctx.performed.add(action.action)
        if fp:
            ctx.action_counts[fp] += 1
            ctx.recent_fps.append(fp)
        ctx.last_action = action
        try:
            note = await run.session.act(space, action)
        except SessionLost:
            return True
        ctx.session_busy = run.session.act_timed_out
        tr.mark("act")
        await heartbeat()  # action applied — the post-action settle/next observe is live work
        # Stamp the action entry with its real step number: the agent cites it as evidence,
        # and the ledger checks citations against step numbers.
        note = note.replace("[step]", f"[step {step_num}]", 1)
        history.append(await hooks.after_action(ctx, action, note))
        ctx.action_note_idx = len(history) - 1
        tr.mark("after_action_hooks")
        run.trace.append(_traced(tr.finish(history)))
        return False

    @staticmethod
    def _result(run: "_Run", driver: Any, final_text: str, final_shot: str | None,
                device_errors: list[str], crashes: list[str], video: str | None) -> ExecutionResult:
        red = run.ctx.redactor
        decider, ledger = run.decider, run.ledger
        # Secrets learned late (typed at step 9, mentioned at step 2) are scrubbed from
        # everything stored at the end; the live log lines already went out per step.
        trace = red.scrub(run.trace)
        steps = [red.scrub_model(s) for s in run.steps]
        return ExecutionResult(
            steps=steps,
            final_text=red.scrub(final_text) or None,
            final_screenshot_path=final_shot,
            diagnostics=Diagnostics(errors=device_errors, crashes=crashes),
            self_verdict=red.scrub_model(run.self_verdict),
            video_path=video,
            usage=Usage(total_tokens=_tokens(decider, ledger),
                        total_cost=getattr(decider, "cost_usd", lambda: None)()),
            error=run.error,
            # Only a checklist the agent actually reported on (a bare-action decider cannot):
            # the verdict holds a PASS to it.
            clauses=red.scrub([c.as_dict() for c in ledger.clauses]) if ledger.active else [],
            usage_extra={"step_trace": trace,
                         "dropped_settings": list(getattr(driver, "dropped_settings", []) or []),
                         "brain_tokens": {
                k: getattr(decider, k, None) for k in (
                    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")}},
        )

    # ── helpers ──

    async def _build_driver(self, spec: ExecutionSpec) -> DeviceDriver:
        if self._driver is not None:
            return self._driver
        if self._provider is None:
            from tamam.executors.ios.providers import build_provider
            self._provider = build_provider(spec, self._services)
        return await self._provider.driver(spec)

    def _planner_for(self, decider: Any) -> Any:
        """The clause planner. Injected wins; a real (model) decider gets the model
        planner; a scripted decider (hermetic tests, no API key) gets the deterministic
        split, so tests never reach the network."""
        if self._planner is not None:
            return self._planner
        if self._decider is None:
            return ModelPlanner(get_settings().agent_model)
        return HeuristicPlanner()

    @staticmethod
    def _unpack(decision: Any, ctx: StepContext, ledger: "ClauseLedger",
                history: list[str], step_num: int) -> tuple[Any, str | None]:
        """A Turn carries the action plus evaluation, notes and clause progress; a bare
        action (scripted/legacy decider) carries only itself."""
        if not isinstance(decision, Turn):
            return decision, None
        if decision.memory.strip():
            ctx.memory = decision.memory.strip()[:600]
        if decision.clause_updates or ledger.clauses:
            problems = ledger.record(decision.clause_updates, step_num)
            if problems:
                history.append("[harness] checklist update rejected: " + "; ".join(problems))
        return decision.action, ctx.memory or None

    def _start_live_governor(self, driver: DeviceDriver) -> asyncio.Task | None:
        if self._live is None:
            return None
        # Local simulator/device: WDA MJPEG stream — off the command session.
        if not getattr(driver, "remote", False) and getattr(driver, "mjpeg_url", None):
            return asyncio.ensure_future(self._mjpeg_governor(driver))
        # Remote (cloud): no local MJPEG, so poll screenshots over the session at a
        # gentle cadence — some live view beats none, at a small contention cost.
        if hasattr(driver, "screenshot_bytes"):
            poll = 2.0 if getattr(driver, "remote", False) else LIVE_POLL_S
            return asyncio.ensure_future(self._screenshot_governor(driver, poll))
        return None

    async def _watching(self) -> bool:
        try:
            return await self._live.viewers() > 0
        except Exception:
            return False

    async def _mjpeg_governor(self, driver: Any) -> None:
        """Stream WDA's MJPEG frames to watchers — off the command session, so it
        never contends with the agent loop. Gated on live viewers."""
        import httpx

        url = driver.mjpeg_url
        try:
            while True:
                if not await self._watching():
                    await asyncio.sleep(0.5)
                    continue
                try:
                    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=None)) as client:
                        async with client.stream("GET", url) as resp:
                            buf = b""
                            async for chunk in resp.aiter_bytes():
                                buf += chunk
                                while True:
                                    s = buf.find(b"\xff\xd8")
                                    e = buf.find(b"\xff\xd9", s + 2) if s != -1 else -1
                                    if s == -1 or e == -1:
                                        break
                                    await self._live.publish_frame(base64.b64encode(buf[s:e + 2]).decode())
                                    buf = buf[e + 2:]
                                if len(buf) > 5_000_000:
                                    buf = b""
                                if not await self._watching():
                                    break
                except Exception:
                    await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            raise

    async def _screenshot_governor(self, driver: Any, poll: float = LIVE_POLL_S) -> None:
        try:
            while True:
                if await self._watching():
                    try:
                        await self._live.publish_frame(_png_to_jpeg_b64(await driver.screenshot_bytes()))
                    except Exception:
                        pass
                await asyncio.sleep(poll)
        except asyncio.CancelledError:
            raise

    async def _decide_with_retry(self, decider, objective, space, history, screenshot,
                                 timeout, heartbeat=_noop_heartbeat):
        """One decision, retrying a transient model/API failure (overloaded, a malformed
        structured output) with backoff. Those are OUR infra, never the app — a run must
        not die on the first 529. A timeout is not retried: it already spent its budget."""
        for attempt in range(_DECIDE_RETRIES + 1):
            try:
                return await asyncio.wait_for(
                    decider.decide(objective, space, history, screenshot), timeout)
            except (asyncio.TimeoutError, TimeoutError):
                raise
            except Exception as exc:
                if attempt == _DECIDE_RETRIES:
                    raise
                logger.warning("decider failed (%s); retry %d/%d", exc, attempt + 1,
                               _DECIDE_RETRIES)
                await heartbeat()
                await asyncio.sleep(_DECIDE_BACKOFF_S * (2 ** attempt))

    async def _start_with_reconnect(self, driver) -> None:
        """Bring the session up, recovering from a device that returns a session which is
        already stale (slow-provisioning cloud / long device wait). start()'s first command
        now raises DriverSessionDead instead of a raw error, so we reconnect on the SAME
        endpoint (no re-provisioning, no extra device-minutes) and retry — rather than
        erroring the whole run before a single step. Bounded by the same reconnect budget
        as mid-run; on exhaustion the DriverSessionDead propagates and the run ERRORs."""
        while True:
            try:
                await driver.start()
                return
            except DriverSessionDead as exc:
                logger.warning("session dead at startup (%s); attempting reconnect", exc)
                if not await self._try_reconnect(driver):
                    raise

    async def _try_reconnect(self, driver) -> bool:
        """Re-establish a dead device session (app/WDA crash), capped so a reliably-
        crashing app can't loop forever. Returns True if a fresh session is ready."""
        if self._reconnects >= self._max_reconnects:
            logger.warning("session died; reconnect budget (%d) exhausted — ending",
                           self._max_reconnects)
            return False
        self._reconnects += 1
        try:
            await driver.reconnect()
            logger.warning("reconnected after session death (%d/%d)",
                           self._reconnects, self._max_reconnects)
            return True
        except Exception as exc:
            logger.warning("reconnect failed (%s); ending with partial evidence", exc)
            return False

    def _safe_logs(self, driver) -> tuple[list, list]:
        """Device/crash logs, tolerant of a driver that never built (None) or errors."""
        if driver is None:
            return [], []
        try:
            return driver.logs()
        except Exception:
            return [], []

    async def _augment_with_grounder(self, grounder, shot_path, space, instructions):
        """Let the grounder add vision-found targets the a11y tree missed, cropped to
        the low-info region. Fail-open: any error (or the NullGrounder default) leaves
        the space untouched. Only runs on vision-fallback steps, so it costs nothing on
        the a11y-happy path."""
        if isinstance(grounder, NullGrounder) or not shot_path:
            return space
        try:
            png = Path(shot_path).read_bytes()
            region = low_info_region(space)
            # OCR maps full-frame pixels → points with a trivial, exact scale (no crop
            # offset), so pass the FULL screenshot; `region` is a hint the grounder may use.
            grounded = await grounder.detect(png, region, space)
            logger.info("grounder(%s) detected %d target(s): %s",
                        type(grounder).__name__, len(grounded),
                        [g.label for g in grounded[:8]])
            if grounded:
                merged = merge_grounded(space, grounded)
                logger.info("grounder added %d target(s) the a11y tree missed",
                            len(merged.elements) - len(space.elements))
                return merged
        except Exception:
            logger.warning("grounder augmentation failed; using a11y space", exc_info=True)
        return space

    def _safe_som(self, shot_path: str | None, space) -> tuple[bytes | None, str | None]:
        """Build the set-of-marks image sent to the model. Returns (bytes, path):
        bytes go to the decider, path is persisted as evidence (the exact annotated
        view the model reasoned over)."""
        if not shot_path:
            return None, None
        try:
            png = Path(shot_path).read_bytes()
            out = Path(shot_path).parent / f"som_{Path(shot_path).stem}.png"
            set_of_marks(png, space, out)
            return out.read_bytes(), str(out)
        except Exception:
            return None, None

    async def _safe_text(self, driver: DeviceDriver) -> str:
        try:
            return await driver.accessibility()
        except Exception:
            return ""

    async def _safe_capture_logs(self, driver: Any) -> None:
        fn = getattr(driver, "capture_diagnostics", None)
        if fn:
            try:
                await fn()
            except Exception:
                pass

    async def _safe_cleanup(self) -> None:
        if self._provider is not None:
            try:
                await self._provider.cleanup()
            except Exception:
                pass


def _png_to_jpeg_b64(png: bytes, max_w: int = 480) -> str:
    from PIL import Image

    img = Image.open(io.BytesIO(png)).convert("RGB")
    if img.width > max_w:
        img = img.resize((max_w, int(img.height * max_w / img.width)))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=50)
    return base64.b64encode(buf.getvalue()).decode()


async def _stop_governor(governor: asyncio.Task | None) -> None:
    if governor is not None:
        governor.cancel()
        try:
            await governor
        except (asyncio.CancelledError, Exception):
            pass


async def _safe_stop(driver: Any) -> str | None:
    try:
        return await driver.stop()
    except Exception:
        return None
