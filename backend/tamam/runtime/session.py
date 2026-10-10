"""The shell: load the run from the DB, compose a HarnessRun from real
collaborators, execute it, persist the result.

Everything framework-shaped lives here — DB reads/writes, the observability root
span, artifact-URL rewriting. The harness core (HarnessRun) stays pure; this is
the only file that wires it to the outside world.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlmodel import select

from tamam.assembly import assemble_run
from tamam.config import get_settings
from tamam.db import get_session
from tamam.executors.base import AppTarget, RunServices, WebTarget
from tamam.executors.registry import graduates, platform_of
from tamam.runtime.routing import SCRIPT, get_executor, select_executor
from tamam.storage import get_storage
from tamam.graduation.lifecycle import (
    after_script_run, current_script, graduate, proof_stamp, run_target,
)
from tamam.runtime import events
from tamam.harness.checks import Check
from tamam.harness.instructions import PROMPTS_VERSION, execution_prompt_for
from tamam.harness.spec import Budgets, RunResult, RunSpec
from tamam.models import Build, Lifecycle, Outcome, RunStepEvent, Test, TestRun
from tamam.observability import span
from tamam.runtime.event_sink import DbEventSink

logger = logging.getLogger(__name__)


async def run_session(run_id: str) -> None:
    rid = uuid.UUID(run_id)
    settings = get_settings()

    prepared = await _prepare(rid, settings)
    if prepared is None:
        return
    spec, executor_id, budgets, queue_wait_s = prepared

    sink = DbEventSink(rid)
    executor = get_executor(executor_id)
    # The host's services for this run: a live view (the executor screencasts only while a
    # viewer is subscribed) and the record of app copies uploaded to a device farm.
    if hasattr(executor, "attach"):
        from tamam.runtime.builds import record_vendor_ref
        from tamam.runtime.live import LivePreview

        executor.attach(RunServices(live=LivePreview(rid, events.get_bus()),
                                    record_upload=record_vendor_ref))
    run = assemble_run(spec, executor, sink, settings=settings)

    async def _finalize(res: RunResult) -> None:
        res.timings.queue_wait_s = queue_wait_s
        # Persist FIRST (the durable source of truth), THEN notify the live bus. The persist
        # is SHIELDED so a second cancellation (worker shutdown mid-teardown) can't interrupt
        # it and leave the run non-terminal — a permanently lost run.
        await asyncio.shield(_persist(rid, res))
        await events.publish_lifecycle(rid, Lifecycle.FINISHED.value)
        await events.get_bus().close(str(rid))

    with span("run", run_id=run_id, executor=executor_id):
        try:
            result = await run.execute()
        except asyncio.CancelledError:
            # Worker shutdown or ARQ job_timeout cancelled us. Persist a terminal ERROR so
            # the run is never left in a non-terminal state, then propagate the cancel.
            logger.warning("Session cancelled for run %s — persisting ERROR", run_id)
            await _finalize(RunResult.errored("Run cancelled (worker shutdown or timeout)"))
            raise
        except Exception as exc:
            logger.exception("Session failed for run %s", run_id)
            result = RunResult.errored(f"Harness error: {exc}")

    await _finalize(result)


async def _prepare(
    rid: uuid.UUID, settings
) -> tuple[RunSpec, str, Budgets, float] | None:
    """Load the run + test, freeze the spec snapshot, mark it starting."""
    async with get_session() as db:
        run = await db.get(TestRun, rid)
        if run is None:
            logger.error("TestRun %s not found", rid)
            return None
        # Idempotency: only a freshly-QUEUED run may execute. An ARQ redelivery (worker died
        # after picking up the job) or a double-enqueue would otherwise re-run the SAME run —
        # provisioning a second real device and overwriting the verdict. Anything past QUEUED
        # has already been claimed or finished, so skip.
        if run.lifecycle != Lifecycle.QUEUED:
            logger.warning("run %s already in lifecycle %s — skipping duplicate execution",
                           rid, run.lifecycle.value)
            return None
        test = await db.get(Test, run.test_id)
        assert test is not None

        executor_id = select_executor(test)
        may_replay = graduates(executor_id) and executor_id != SCRIPT
        script = await current_script(db, test) if may_replay else None
        if script is not None:
            executor_id = "script"   # fast replay; a full AI run is its safety net
        # Resolve the app under test: a Project build (preferred) or the legacy
        # inline path. The bundle id falls back to the build's.
        app_build_ref = test.app_build_ref
        app_bundle_id = test.app_bundle_id
        if test.build_id is not None:
            build = await db.get(Build, test.build_id)
            if build is not None:
                app_build_ref = build.app_path
                app_bundle_id = app_bundle_id or build.bundle_id
        budgets = Budgets.from_settings(
            settings,
            max_steps=run.spec_snapshot.get("max_steps", settings.default_max_steps),
            max_wall_clock_s=run.spec_snapshot.get(
                "max_wall_clock_s", settings.default_max_wall_clock_s
            ),
        )
        if script is not None:
            # The replay's own steps and time come on top of a possible full AI run.
            n = len((script.body or {}).get("steps") or [])
            budgets = dataclasses.replace(budgets, max_steps=budgets.max_steps + n + 2,
                                          max_wall_clock_s=budgets.max_wall_clock_s + 60 + 15 * n)
        platform = platform_of(executor_id)
        target = (WebTarget(start_url=test.start_url) if platform == "web" else AppTarget(
            build_ref=app_build_ref, build_id=str(test.build_id) if test.build_id else None,
            bundle_id=app_bundle_id, device=test.device, os_version=test.ios_platform_version,
            backend=test.ios_target, reuse_session=test.reuse_session))
        spec = RunSpec(
            instructions=test.instructions,
            execution_prompt=execution_prompt_for(platform, test.start_url, test.instructions),
            platform=platform,
            target=target,
            checks=[Check.model_validate(c) for c in (test.checks or [])],
            checks_exhaustive=test.checks_exhaustive,
            budgets=budgets,
            script=script.body if script is not None else None,
            script_status=script.status if script is not None else None,
            script_id=str(script.id) if script is not None else None,
        )

        run.executor_id = executor_id
        run.spec_snapshot = {
            "start_url": test.start_url,
            "instructions": test.instructions,
            "max_steps": budgets.max_steps,
            "max_wall_clock_s": budgets.max_wall_clock_s,
            "executor_id": executor_id,
            "script_id": str(script.id) if script is not None else None,
            "build_id": str(test.build_id) if test.build_id else None,
            "target": run_target(test),
            # Where it ran (build/target/device/OS) and the script epoch it replayed: a script
            # is only promoted by runs on the place it is proving itself on.
            "proof": proof_stamp(test),
            "script_epoch": script.epoch if script is not None else None,
            "prompts_version": PROMPTS_VERSION,
            # Who decides verdicts the deterministic checks leave open — the UI shows it.
            "verdict_source": "judge" if settings.judge_enabled else "agent",
        }
        run.lifecycle = Lifecycle.STARTING
        run.started_at = datetime.now(UTC)
        run.status_message = f"Starting {executor_id} executor"
        created = run.created_at if run.created_at.tzinfo else run.created_at.replace(tzinfo=UTC)
        queue_wait_s = round((run.started_at - created).total_seconds(), 2)
        await db.commit()

    await events.publish_lifecycle(rid, Lifecycle.STARTING.value)
    return spec, executor_id, budgets, queue_wait_s


_NON_TERMINAL = (Lifecycle.QUEUED, Lifecycle.STARTING, Lifecycle.RUNNING, Lifecycle.JUDGING)


async def reap_stale_runs(max_age_s: float | None = None) -> int:
    """Mark runs stuck in a non-terminal lifecycle past a generous deadline as terminal
    ERROR. This is the recovery path for a run whose worker died / was OOM-killed / hit
    ARQ's job_timeout, or a QUEUED job the queue lost — otherwise it would sit non-terminal
    forever (a lost run). The deadline is well beyond any legitimate run (wall-clock +
    startup grace + judge headroom), so a live run is never reaped out from under itself.
    Idempotent and safe to call periodically."""
    settings = get_settings()
    if max_age_s is None:
        max_age_s = settings.default_max_wall_clock_s + settings.startup_timeout_s + 120
    cutoff = datetime.now(UTC) - timedelta(seconds=max_age_s)

    def _aware(dt: datetime | None) -> datetime | None:
        if dt is None:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)

    async with get_session() as db:
        rows = await db.execute(select(TestRun).where(TestRun.lifecycle.in_(_NON_TERMINAL)))
        reaped = 0
        for run in rows.scalars().all():
            # Age from started_at once running; fall back to created_at for QUEUED orphans
            # that never started.
            ref = _aware(run.started_at) or _aware(run.created_at)
            if ref is None or ref >= cutoff:
                continue
            run.lifecycle = Lifecycle.FINISHED
            run.outcome = Outcome.ERROR
            run.status_message = "Run abandoned (worker crash, shutdown, or timeout) — reaped"
            run.finished_at = datetime.now(UTC)
            reaped += 1
        if reaped:
            await db.commit()
            logger.warning("reaper: marked %d stale run(s) as ERROR", reaped)
    return reaped


async def _persist(rid: uuid.UUID, result: RunResult) -> None:
    async with get_session() as db:
        run = await db.get(TestRun, rid)
        if run is None:
            return
        run.lifecycle = Lifecycle.FINISHED
        run.outcome = result.outcome
        run.status_message = result.reason
        run.disputed = result.disputed
        run.self_verdict = result.self_verdict
        run.judge_verdict = result.judge_verdict
        run.usage = {**result.usage, "timings": _timings_dict(result)}
        records: list[dict] = []
        run.video_url = await _store_artifact(
            rid, "recording.mp4", result.evidence.video_path if result.evidence else None
        )
        if result.evidence is not None:
            run.clauses = list(result.evidence.clauses or []) or None
            run.trace_key = await _store_trace(rid, result.evidence)
            records = (result.evidence.usage_extra or {}).get("step_trace") or []
        run.finished_at = datetime.now(UTC)
        if result.evidence is not None:
            await _rescrub_steps(db, rid, result.evidence)
            await _attach_screenshots(db, rid, result.evidence)
        executor_id = run.executor_id   # read before commit: the row expires with the session
        await db.commit()
    if result.evidence is not None and graduates(executor_id):
        await _graduate(rid, records, result.evidence)


async def _graduate(rid, records: list[dict], evidence) -> None:
    """Fast-mode compile, AFTER the verdict is committed and in its own session: a failure
    here (a version race, a bug) can never cost the run its verdict."""
    info: dict | None = None
    try:
        async with get_session() as db:
            run = await db.get(TestRun, rid)
            if run is None:
                return
            info = (evidence.usage_extra or {}).get("graduation")
            decision: dict = {}
            if info:
                decision = await after_script_run(db, run, info)
            mode = (info or {}).get("mode")
            replay_ok = ((info or {}).get("replay") or {}).get("verdict") == "passed"
            # Compile a (replacement) script from the AI run's trace — but not when the script
            # just did its job: a fast run, or a probation run where replay and AI agreed.
            # A hand-off's AI trace starts MID-TEST (replay did the first steps), so it can
            # never be compiled into a whole script; the next full AI run will re-graduate.
            if records and mode != "handoff" and not (mode == "fast" or (
                    mode == "probation" and replay_ok and decision.get("agree"))):
                # A proven (active) script stays until its replacement earns trust or it is
                # retired after repeated misses.
                compiled = await graduate(db, run, records, run.clauses or [],
                                          run_error=evidence.error)
                decision = {**decision, **(compiled or {})}
            run.graduation = decision or None
            db.add(run)
            await db.commit()
    except Exception:
        logger.warning("fast-mode compile failed for run %s", rid, exc_info=True)
        try:
            async with get_session() as db:
                run = await db.get(TestRun, rid)
                if run is not None:
                    # Keep the replay's story (mode, report): only the compile step failed.
                    kept = {k: v for k, v in (info or {}).items() if k in ("mode", "replay")}
                    run.graduation = {**kept, "ready": False,
                                      "reasons": ["Fast mode could not be prepared for this run."]}
                    await db.commit()
        except Exception:
            logger.warning("could not record the fast-mode decision for run %s", rid, exc_info=True)


def _timings_dict(result: RunResult) -> dict:
    t = result.timings
    return {"queue_wait_s": t.queue_wait_s, "execute_s": t.execute_s, "verify_s": t.verify_s}


async def _store_artifact(rid, name: str, path: str | None) -> str | None:
    """Upload a locally-produced artifact into the (private) storage layer and
    return its opaque KEY. Remote URLs (a legacy cloud pass-through) are returned
    as-is. The DB only ever holds a key or a URL — never a local filesystem path."""
    if not path:
        return None
    if path.startswith(("http://", "https://")):
        return path
    key = f"runs/{rid}/{name}"
    await get_storage().put(Path(path), key)
    return key


async def _store_trace(rid, evidence) -> str | None:
    """Upload the run's per-step trace (JSON lines) to private storage; return its key.
    The executor has already replaced typed text with a marker. Best effort: a storage
    hiccup must not cost the run its verdict."""
    steps = (evidence.usage_extra or {}).get("step_trace") or []
    if not steps:
        return None
    fh = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    try:
        with fh:
            for s in steps:
                fh.write(json.dumps(s, default=str) + "\n")
        return await _store_artifact(rid, "step_trace.jsonl", fh.name)
    except Exception:
        logger.warning("could not store the step trace for run %s", rid, exc_info=True)
        return None
    finally:
        Path(fh.name).unlink(missing_ok=True)


async def _rescrub_steps(db, rid, evidence) -> None:
    """Step events are stored as they stream; a value can be recognised as secret only later in
    the run (mentioned at step 2, typed into a password field at step 9). The executor returns
    its steps scrubbed with EVERYTHING it learned, so the stored rows are replaced with those.
    (Live viewers already saw the early frames; nothing stored keeps them.)"""
    final = {e.step: e.model_dump() for e in (evidence.steps or [])}
    if not final:
        return
    rows = (await db.execute(select(RunStepEvent).where(RunStepEvent.run_id == rid))).scalars().all()
    for row in rows:
        clean = final.get(row.step)
        if clean is not None and row.payload != clean:
            row.payload = clean


async def _attach_screenshots(db, rid, evidence) -> None:
    """The live stream has no screenshots; the final evidence bundle does. Merge.
    Each image is uploaded to (private) storage and the row keeps only its opaque KEY
    — the raw screen AND, when the model was shown one, the set-of-marks annotated
    image it actually reasoned over. Both are served later as short-lived signed URLs,
    minted only after the ownership check in get_run (same private path — an app
    screenshot is customer IP)."""
    shots: dict[int, str] = {}
    annotated: dict[int, str] = {}
    for e in evidence.steps:
        if e.screenshot_path:
            shots[e.step] = await _store_artifact(rid, f"step-{e.step}.png", e.screenshot_path)
        if e.annotated_screenshot_path:
            annotated[e.step] = await _store_artifact(
                rid, f"som-{e.step}.png", e.annotated_screenshot_path
            )
    if not shots and not annotated:
        return
    rows = (
        await db.execute(select(RunStepEvent).where(RunStepEvent.run_id == rid))
    ).scalars().all()
    seen = set()
    for row in rows:
        if row.step in shots:
            row.screenshot_url = shots[row.step]
        if row.step in annotated:
            row.annotated_url = annotated[row.step]
        seen.add(row.step)
    for step in set(shots) | set(annotated):  # steps that never streamed (e.g. guardrail abort)
        if step not in seen:
            db.add(RunStepEvent(
                run_id=rid, step=step, payload={},
                screenshot_url=shots.get(step), annotated_url=annotated.get(step),
            ))
