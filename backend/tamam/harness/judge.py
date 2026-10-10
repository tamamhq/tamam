"""AnthropicJudge — the concrete Judge (Verification layer 2, maker/checker).

A separate agent that grades the evidence bundle. It never sees the executor's
self-verdict (no anchoring). Injected into the Verifier, so the core never
depends on a live model — tests pass a fake Judge instead.

Native Pydantic AI guardrails on the call: schema-validated output with retry,
a semantic output validator (failed verdicts must carry repro steps), and a
usage ceiling — the judge has a leash too.
"""

from __future__ import annotations

import asyncio
import json
import logging
from functools import lru_cache
from pathlib import Path

from pydantic_ai import Agent, BinaryContent, ModelRetry
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits

from tamam.config import get_settings
from tamam.executors.base import ExecutionResult
from tamam.harness.instructions import build_judge_prompt
from tamam.harness.spec import JudgeResult
from tamam.harness.verdict import Verdict

logger = logging.getLogger(__name__)

MAX_DOM_CHARS = 6000
MAX_STEPS_IN_EVIDENCE = 40

# The judge's own structural guardrail: bounded requests + tokens. A judge that
# loops or rambles is an infra error, not a verdict. The token ceiling must comfortably
# fit a long run's evidence — the step log + up to 5 journey screenshots + the settled
# final screenshot — across BOTH the strict and lenient attempts (see evaluate). 30k
# was too low: a legitimate 25-step run (many images) hit 31.7k and errored a PASS.
# Haiku's context is 200k, so headroom is cheap.
JUDGE_USAGE_LIMITS = UsageLimits(request_limit=6, total_tokens_limit=120_000)


def build_evidence(result: ExecutionResult) -> str:
    """Serialize the evidence bundle for the judge. Excludes self_verdict by design."""
    steps = [
        {"step": s.step, "url": s.url, "next_goal": s.next_goal, "actions": s.actions}
        for s in result.steps[:MAX_STEPS_IN_EVIDENCE]
    ]
    evidence = {
        "step_log": steps,
        "final_url": result.final_url,
        "final_page_text": (result.final_text or "")[:MAX_DOM_CHARS],
        # Key names kept: they are part of the judge's prompt (crashes reported as before).
        "console_errors": result.diagnostics.errors[:20],
        "failed_requests": (result.diagnostics.failed_requests
                            + [{"crash": c} for c in result.diagnostics.crashes])[:20],
    }
    return json.dumps(evidence, ensure_ascii=False, default=str)


def _journey_screenshots(result: ExecutionResult, limit: int = 8) -> list[tuple[str, bytes]]:
    """The screens the agent saw (in order) so the judge can verify intermediate screens the
    agent navigated away from — PLUS the settled final screen (captured after the loop) as
    the authoritative end state. Per-step shots are captured before their action lands, so
    the last one can be mid-transition; the settled shot is what success/failure is graded on.
    Deduplicates consecutive identical shots, then SAMPLES EVENLY across the whole journey
    (not just the tail) so an objective about an early/mid-run screen — 'added 3 items THEN
    applied a discount' — is actually verifiable, not graded on the last few frames alone."""
    shots: list[tuple[str, bytes]] = []
    prev: bytes | None = None
    for step in result.steps:
        p = step.screenshot_path
        if p and Path(p).exists():
            data = Path(p).read_bytes()
            if data != prev:
                shots.append((f"step {step.step}", data))
                prev = data
    if len(shots) > limit:
        # Keep first + last of the sequence and spread the rest evenly across the middle,
        # so coverage spans the run instead of clustering at the end.
        idxs = sorted({round(i * (len(shots) - 1) / (limit - 1)) for i in range(limit)})
        shots = [shots[i] for i in idxs]
    fp = result.final_screenshot_path
    if fp and Path(fp).exists():
        data = Path(fp).read_bytes()
        if not shots or shots[-1][1] != data:  # dedup vs a settled last-step shot
            shots.append(("final (settled end state)", data))
    return shots


@lru_cache(maxsize=16)  # one agent per (model, prompt, strict) triple, not per call
def _build_agent(model: str, instructions: str, strict: bool = True) -> Agent:
    # temperature=0 requests maximum reproducibility — the same evidence bundle should
    # grade the same way. It's best-effort: older models honor it; newer ones (Sonnet 5,
    # Opus 4.x) don't expose temperature and silently ignore it. So the ACTUAL guardrail on
    # grader stability + quality is a scored gold-set eval of the judge (run before
    # any judge-prompt change), not this flag alone.
    agent: Agent[None, Verdict] = Agent(
        model, output_type=Verdict, system_prompt=build_judge_prompt(instructions),
        model_settings=ModelSettings(temperature=0.0),
    )
    if strict:
        # Quality nudge: a failed verdict should carry repro steps. This is a RETRY,
        # not a hard gate — some models (notably Haiku) intermittently emit repro_steps
        # as text inside `reasoning` instead of the JSON field, leaving it empty; the
        # retry lets them correct. If they can't, evaluate() falls back to the lenient
        # agent rather than letting the retry exhaust and ERROR the whole run.
        @agent.output_validator
        def _failed_needs_repro(verdict: Verdict) -> Verdict:
            if not verdict.passed and not verdict.repro_steps:
                raise ModelRetry("Failed verdicts must include 3-6 concrete repro steps.")
            return verdict

    return agent


class AnthropicJudge:
    """Judge implementation backed by Pydantic AI + Claude."""

    async def evaluate(self, instructions: str, result: ExecutionResult) -> JudgeResult:
        model = get_settings().judge_model
        parts: list = [build_evidence(result)]
        shots = _journey_screenshots(result)
        if shots:
            parts.append(
                f"\n{len(shots)} screenshots follow, in order — the screens the agent "
                "saw during the run. The LAST is the settled end state (after the final "
                "action completed); grade the objective's success on that, and treat a "
                "transient banner/spinner in an earlier mid-transition frame as such, "
                "not a failure, if the settled screen shows success. Use the earlier "
                "shots to verify any intermediate screen the objective refers to."
            )
            for label, data in shots:
                parts.append(f"Screenshot — {label}:")
                parts.append(BinaryContent(data=data, media_type="image/png"))

        # Bound the judge call: it runs outside the execute-loop guardrails, so a wedged
        # model call would hang the run in JUDGING indefinitely. On timeout we let the
        # error propagate → the run resolves to ERROR (inconclusive), never a false verdict.
        timeout_s = get_settings().judge_timeout_s

        async def _run_judge(strict: bool):
            return await asyncio.wait_for(
                _build_agent(model, instructions, strict=strict).run(
                    parts, usage_limits=JUDGE_USAGE_LIMITS),
                timeout=timeout_s,
            )

        try:
            run = await _run_judge(strict=True)
        except UnexpectedModelBehavior as exc:
            # The strict repro-steps retry exhausted (the model keeps malforming that
            # field). A verdict WITHOUT repro steps is far more useful than ERRORing the
            # whole run — rerun once without the strict validator so a real verdict lands.
            # Log the exception type only, never `parts` (the objective can carry creds).
            logger.warning("judge strict output failed (%s) — retrying lenient", type(exc).__name__)
            run = await _run_judge(strict=False)

        usage = run.usage  # RunUsage attribute in pydantic-ai 2.x
        return JudgeResult(
            verdict=run.output,
            usage={
                "judge_tokens": getattr(usage, "total_tokens", None),
                "judge_requests": getattr(usage, "requests", None),
            },
        )
