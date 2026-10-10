"""The harness — a composable core, not a procedure.

A HarnessRun (run.py) is assembled from injected collaborators and orchestrates
one test execution. It touches no DB and no framework; the shell (tamam/runtime/)
wires it to the outside world.

    contracts.py     Clock · Guardrail · EventSink · Judge protocols + RunState
    spec.py          RunSpec / RunResult / Budgets / Timings value objects
    run.py           HarnessRun — the core (owns guardrail enforcement + orchestration)
    guardrails.py    StepLimit · WallClock · StuckWatchdog — pure policies
    verification.py  Verifier — checks engine + injected Judge → Verdicts
    checks.py        deterministic rule engine (@check registry, zero tokens)
    judge.py         AnthropicJudge — the Judge implementation (Pydantic AI)
    verdict.py       Resolution + resolvers — pure code, never LLM
    instructions.py  prompt builders + PROMPTS_VERSION (eval-gated)
    events.py        the live bus (SSE) the DbEventSink publishes to

Composition happens in tamam/runtime/session.py:
    HarnessRun(spec, executor=…, guardrails=[…], verifier=Verifier(judge=…), events=…)

Two laws: (1) code before prompt — rules first, judge for the residue, prompt
edits gated by the eval seed suite; (2) the executor never grades its own work —
its self-verdict is a signal, the judge is authoritative.
"""
