"""ScriptExecutor — run a test from its compiled script, with a full AI run as the safety net.

  active script     : replay. passed → PASS (fast). failed (a locked check on its own screen)
                      → FAIL (an app bug). miss / error → a full AI run decides.
  probation script  : replay, then ALWAYS a full AI run too; the AI's verdict decides and the
                      two are compared (agreement is how a script earns "active").
The mode, the replay report and (for probation) the agreement go in usage_extra["graduation"],
where the post-run lifecycle reads them.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from tamam.config import get_settings
from tamam.executors.base import (ExecutionResult, ExecutionSpec, RunServices, SelfVerdict, StepEvent,
                                  Usage)
from tamam.executors.core.redact import Redactor
from tamam.graduation.look import ClauseLooker
from tamam.graduation.platform import OpenDevice, ReplayPlatform
from tamam.graduation.replay import ReplayOutcome, ReplayRunner
from tamam.graduation.script import SCRIPT_SCHEMA, CompiledScript
from tamam.harness.run import GuardrailTripped

logger = logging.getLogger(__name__)


async def _noop_heartbeat() -> None:
    return None


class ScriptExecutor:
    executor_id = "script"

    def __init__(self, platform: ReplayPlatform, ai: Any = None, looker: Any = None) -> None:
        self._platform = platform        # the device side: open a device, build the AI agent
        self._ai = ai                    # injectable for tests; default: the platform's agent
        self._looker = looker
        self._replay_steps: list = []

    def attach(self, services: RunServices) -> None:
        """The host's services, for replay and a hand-off (live view, farm uploads)."""
        self._platform.attach(services)

    async def execute(self, spec: ExecutionSpec, on_event, heartbeat=_noop_heartbeat) -> ExecutionResult:
        try:
            script = CompiledScript.model_validate(spec.script or {})
        except Exception:
            logger.warning("unreadable script; running with AI", exc_info=True)
            return await self._ai_run(spec, on_event, heartbeat, mode="fallback",
                                      replay={"verdict": "miss", "reason": "The saved script could not be read."})
        if script.schema_ > SCRIPT_SCHEMA:
            return await self._ai_run(spec, on_event, heartbeat, mode="fallback",
                                      replay={"verdict": "miss", "reason": "The script is newer than this server."})

        looker = self._looker or ClauseLooker(get_settings().agent_model, test_text=spec.instructions)
        red = Redactor(spec.instructions)
        probation = (spec.script_status or "probation") != "active"
        outcome, steps, final_text, final_shot, video, handle = await self._replay(
            spec, script, looker, on_event, heartbeat, keep_on_miss=not probation)
        outcome.reason = red.scrub(outcome.reason)
        for c in outcome.checks:
            c.detail = red.scrub(c.detail)
        report = outcome.as_dict()
        report["ai_looks"] = getattr(looker, "calls", 0)
        g = {"replay": report, "script_id": spec.script_id}

        if outcome.cancelled:
            # A guardrail ended the run during replay: keep the evidence, start nothing new.
            return ExecutionResult(steps=steps, video_path=video, error=outcome.reason,
                                   usage_extra={"graduation": {"mode": "fast", **g}})
        self._replay_steps = steps
        if handle is not None:
            # An ACTIVE script missed: the AI takes over on the SAME device, from the screen
            # replay reached — no second device session, no restart.
            base = max(1, spec.max_steps - len(script.steps) - 2)
            res = await self._handoff(spec.model_copy(update={"max_steps": base}), script, outcome,
                                      handle, on_event, heartbeat, report, len(steps), looker)
            return res
        if probation or outcome.verdict in ("miss", "error"):
            mode = "probation" if probation else "fallback"
            # The AI run gets the ordinary step budget; the replay's steps were extra.
            base = max(1, spec.max_steps - len(script.steps) - 2)
            res = await self._ai_run(spec.model_copy(update={"max_steps": base}), on_event, heartbeat,
                                     mode=mode, replay=report, offset=len(steps), looker=looker)
            # The verdict is the AI run's: judge it on ITS steps only (a replayed step must not
            # satisfy a check like action_used for an AI run that never did it). The replay's
            # steps were already streamed to the run's timeline.
            if video and not res.video_path:
                res.video_path = video
            return res
        passed = outcome.verdict == "passed"
        return ExecutionResult(
            steps=steps, final_text=red.scrub(final_text) or None, final_screenshot_path=final_shot,
            video_path=video, clauses=clause_ledger(script, outcome),
            self_verdict=SelfVerdict(passed=passed, reason=outcome.reason[:300]),
            usage=Usage(total_tokens=getattr(looker, "input_tokens", 0) + getattr(looker, "output_tokens", 0),
                        total_cost=_cost(looker)),
            usage_extra={"graduation": {"mode": "fast", "replay": report,
                                        "script_id": spec.script_id}},
        )

    async def _replay(self, spec, script, looker, on_event, heartbeat, keep_on_miss=False):
        """Replay; returns (outcome, steps, final_text, final_shot, video, handle). On a MISS with
        `keep_on_miss`, the device is left open and `handle` is it, for the AI to take over in
        place — the caller then owns stopping and releasing it."""
        handle: OpenDevice | None = None
        device: OpenDevice | None = None
        outcome = None
        steps: list[StepEvent] = []
        final_text, final_shot, video = "", None, None
        try:
            device = await self._platform.open(spec)
            session = device.session

            async def on_step(step, space, note, shot):  # noqa: ANN001
                # The same action names the AI run records (checks such as action_used read them).
                name = {"background": "background_app"}.get(step.do, step.do)
                ev = StepEvent(step=len(steps) + 1, next_goal=f"Fast replay: {step.say}",
                               actions=[{"action": name, "replay": True, "step": step.id}],
                               screen_signature=space.source_hash,
                               action_fingerprint=f"replay:{step.id}", screenshot_path=shot)
                steps.append(ev)
                await on_event(ev)
                await heartbeat()

            async def on_look(clause, verdict, why, shot):  # noqa: ANN001
                # The screen the AI look judged is kept as evidence on the run.
                ev = StepEvent(step=len(steps) + 1, next_goal=f"AI look: {clause}",
                               memory=f"{verdict}: {why}"[:300],
                               actions=[{"action": "ai_look", "replay": True, "verdict": verdict}],
                               action_fingerprint=f"look:{len(steps)}", screenshot_path=shot)
                steps.append(ev)
                await on_event(ev)
                await heartbeat()

            outcome = await ReplayRunner(session, script, spec.instructions, clause_look=looker,
                                         heartbeat=heartbeat, on_step=on_step, on_look=on_look).run()
            if outcome.verdict in ("passed", "failed"):
                final_text, final_shot = await session.final(heartbeat)
        except GuardrailTripped as exc:
            # A run budget was breached: a fallback would breach it again. End with evidence.
            outcome = ReplayOutcome("error", f"Run stopped: {exc}")
            outcome.cancelled = True
        except asyncio.CancelledError:
            logger.info("replay cancelled by a guardrail; preserving partial evidence")
            outcome = ReplayOutcome("error", "Run stopped by a guardrail during replay.")
            outcome.cancelled = True
        except Exception as exc:
            logger.warning("replay could not run: %s", exc, exc_info=True)
            outcome = ReplayOutcome("error", f"Replay could not run on the device ({type(exc).__name__}).")
        finally:
            keep = (keep_on_miss and device is not None and outcome is not None
                    and outcome.verdict == "miss" and not outcome.cancelled)
            if keep:
                handle = device
            elif device is not None:
                video = await device.stop()   # the recording, kept as the run's evidence
                await device.release()
        return outcome, steps, final_text, final_shot, video, handle

    async def _handoff(self, spec, script, outcome, handle, on_event, heartbeat, report,
                       offset, looker) -> ExecutionResult:
        device = handle
        started = False   # once the AI executor runs, it owns stopping the driver
        try:
            preset = proven_clauses(script, outcome)
            if not preset:
                raise _NoHandoff("no checklist to hand over")
            # What replay itself proved — captured now: the AI updates the same checklist after.
            by_replay = [c.text for c in preset if c.status == "done"]
            done = [s.say for s in script.steps[:outcome.steps_done]]
            note = ("[harness] Fast replay ran the recorded steps"
                    + (f" ({'; '.join(done)})" if done else "") + f". It stopped: {outcome.reason} "
                    "Continue from the CURRENT screen to finish the test. Clauses already verified "
                    "during replay are checked off on the checklist; evidence the rest yourself.")
            if any(k in outcome.reason for k in ("did not go through", "changed nothing")):
                note += (" The step where replay stopped was ATTEMPTED and may have taken effect: "
                         "check the screen before repeating it.")
            ai = self._platform.agent(device, preset_clauses=preset, seed_history=[note])

            async def shifted(ev: StepEvent) -> None:
                await on_event(ev.model_copy(update={"step": ev.step + offset}))
            started = True
            res = await ai.execute(spec, shifted, heartbeat)   # stops the driver at its end
        except Exception as exc:
            if started:
                raise
            # The hand-off could not start: a full AI run on a fresh session still decides.
            logger.warning("hand-off could not start (%s); running a full AI run", exc)
            res = None
        finally:
            # Release the device whatever happened (a second stop is harmless), then the farm
            # session — a live farm device keeps billing.
            await device.stop()
            await device.release()
        if res is None:
            return await self._ai_run(spec, on_event, heartbeat, mode="fallback", replay=report,
                                      offset=offset, looker=looker)
        # The run's evidence is BOTH parts: replay did the first steps (a check such as
        # action_used, or the judge, must see them), the AI the rest.
        res.steps = list(self._replay_steps) + [s.model_copy(update={"step": s.step + offset})
                                                for s in res.steps]
        res.usage_extra = {**(res.usage_extra or {}), "graduation": {
            "mode": "handoff", "replay": report, "script_id": spec.script_id,
            "proven_by_replay": by_replay}}
        if getattr(looker, "calls", 0):
            tokens = getattr(looker, "input_tokens", 0) + getattr(looker, "output_tokens", 0)
            res.usage = Usage(total_tokens=(res.usage.total_tokens if res.usage else 0) + tokens,
                              total_cost=((res.usage.total_cost or 0.0) if res.usage else 0.0)
                              + (_cost(looker) or 0.0))
        return res

    async def _ai_run(self, spec, on_event, heartbeat, *, mode: str, replay: dict,
                      offset: int = 0, looker: Any = None) -> ExecutionResult:
        """A full AI run on a fresh session (a clean start after the replay). Provisioning a
        second device (~150 s on a farm) sends no events, so the run is kept visibly alive
        until the AI run's first step — or the stuck-watchdog would kill it."""
        ai = self._ai or self._platform.agent()
        first_event = asyncio.Event()

        async def shifted(ev: StepEvent) -> None:
            first_event.set()
            await on_event(ev.model_copy(update={"step": ev.step + offset}) if offset else ev)

        async def keep_alive() -> None:
            while not first_event.is_set():
                await heartbeat()
                try:
                    await asyncio.wait_for(first_event.wait(), timeout=20)
                except asyncio.TimeoutError:
                    continue

        pinger = asyncio.ensure_future(keep_alive())
        try:
            res = await ai.execute(spec, shifted, heartbeat)
        finally:
            first_event.set()
            pinger.cancel()
        res.steps = [s.model_copy(update={"step": s.step + offset}) for s in res.steps]
        g = {"mode": mode, "replay": replay, "script_id": spec.script_id,
             # The AI run's own verdict, None when it did not reach one (infra trouble): the
             # lifecycle judges agreement on the run's FINAL outcome, never on an error.
             "ai_passed": (bool(res.self_verdict.passed) if res.self_verdict and not res.error else None)}
        res.usage_extra = {**(res.usage_extra or {}), "graduation": g}
        if looker is not None and getattr(looker, "calls", 0):
            tokens = getattr(looker, "input_tokens", 0) + getattr(looker, "output_tokens", 0)
            cost = _cost(looker) or 0.0
            res.usage = Usage(total_tokens=(res.usage.total_tokens if res.usage else 0) + tokens,
                              total_cost=((res.usage.total_cost or 0.0) if res.usage else 0.0) + cost)
        return res


def clause_ledger(script: CompiledScript, outcome: ReplayOutcome) -> list[dict]:
    """The run's checklist, in the same shape AI runs record, saying HOW each clause was
    verified: by code, by an AI look on the replayed screen, or by replaying its steps."""
    results = {}
    for r in outcome.checks:
        results.setdefault(r.clause, []).append(r)
    out = []
    for c in script.coverage:
        rs = results.get(c.clause, [])
        if c.kind == "action":
            status = "done" if outcome.verdict == "passed" else "pending"
            out.append({"id": c.clause_id, "text": c.clause, "status": status, "step": None,
                        "how": "replayed", "evidence": "The recorded steps were replayed."})
            continue
        ok = bool(rs) and all(r.ok for r in rs)
        how = "code" if c.checked_by_code else "ai"
        evidence = "; ".join(r.detail for r in rs if r.detail) or (
            "Checked in code on the replayed screen." if how == "code" and ok else "")
        out.append({"id": c.clause_id, "text": c.clause,
                    "status": "done" if ok else ("failed" if rs else "pending"),
                    "step": None, "how": how, "evidence": evidence[:300]})
    return out


class _NoHandoff(Exception):
    """A hand-off is not possible (e.g. no checklist); a fresh full AI run decides instead."""


def proven_clauses(script: CompiledScript, outcome: ReplayOutcome) -> list:
    """The checklist for a hand-off: every clause of the script, with the ones replay PROVED —
    verification clauses whose every locked code check ran and held — marked done. Action
    clauses are never pre-marked: a step that "ran" may not have had its effect (often the
    very reason replay missed); the AI confirms them from what the screen shows."""
    from tamam.harness.clauses import Clause

    ok_by_clause: dict[str, list[bool]] = {}
    for r in outcome.checks:
        if r.how == "code":
            ok_by_clause.setdefault(r.clause, []).append(r.ok)
    out = []
    for i, c in enumerate(script.coverage, 1):
        cl = Clause(id=f"c{i}", text=c.clause)
        if c.kind == "verification" and c.checked_by_code:
            oks = ok_by_clause.get(c.clause, [])
            # Every locked check of the clause must have run and held (none skipped).
            if len(oks) < sum(1 for k in script.checks if k.clause_id == c.clause_id):
                oks = []
            if oks and all(oks):
                cl.status, cl.evidence, cl.how = "done", "checked in code during fast replay", "code"
        out.append(cl)
    return out


def _cost(looker: Any) -> float | None:
    fn = getattr(looker, "cost_usd", None)
    try:
        return fn() if fn else None
    except Exception:
        return None
