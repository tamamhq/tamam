# How a run works

A run is one execution of one test. This page follows it from the API call to the stored
verdict.

```
POST /api/tests/{id}/runs
   │
   ▼
dispatch            queue it (Redis + arq worker) or run it inline (no Redis)
   │
   ▼
run_session         load the test, pick an executor, freeze the spec      runtime/session.py
   │
   ▼
HarnessRun          run the executor under guardrails, then verify       harness/run.py
   │   ├── executor.execute()   the agent (or a replay) does the test, streaming steps
   │   └── Verifier.verify()    checks in code → judge or agent verdict
   ▼
persist             outcome, reason, evidence, artifacts → DB; graduation bookkeeping
```

## 1. Prepare (`runtime/session.py: _prepare`)

- **Idempotent start.** Only a run still in `QUEUED` may execute. A redelivered job (a worker
  died after picking it up) is skipped, so a run never provisions two devices or overwrites
  its own verdict.
- **Pick the executor.** `runtime/routing.py: select_executor` uses the test's
  `executor_id` when set; otherwise the default executor (the iOS agent, unless a plugin
  registers another default). If the test has a graduated script that may replay
  (`graduation/lifecycle.py: current_script`), the executor becomes `script` — fast replay,
  with a full AI run as its safety net. See [Test graduation](test-graduation.md).
- **Resolve the app.** A test points at an uploaded build (preferred) or a legacy path.
- **Budgets.** Max steps and wall-clock time come from the run request or the defaults
  (`TAMAM_DEFAULT_MAX_STEPS`, `TAMAM_DEFAULT_MAX_WALL_CLOCK_S`). A replay gets extra room on top,
  because a miss may still need a full AI run.
- **Freeze the spec.** What was run (instructions, budgets, executor, script, prompt version)
  is snapshotted on the run row, so a verdict can always be traced back to its inputs.

## 2. Execute under guardrails (`harness/run.py: HarnessRun`)

`HarnessRun` touches no database and no framework; every collaborator is injected, so the
whole core runs in unit tests with fakes. It runs the executor and, on every step event,
evaluates **guardrails** (`harness/guardrails.py`):

| Guardrail | Stops the run when | Setting |
|---|---|---|
| `StepLimit` | the step budget is used up | `TAMAM_DEFAULT_MAX_STEPS` |
| `WallClock` | the time budget is used up | `TAMAM_DEFAULT_MAX_WALL_CLOCK_S` |
| `StuckWatchdog` | no step event for too long (or no first step at all) | `TAMAM_STUCK_TIMEOUT_S`, `TAMAM_STARTUP_TIMEOUT_S` |
| `NoProgress` | N actions in a row left the screen unchanged | `TAMAM_NO_PROGRESS_LIMIT` |
| `RepeatedAction` | the same action N times in a row, or N times in total | `TAMAM_REPEATED_ACTION_LIMIT`, `TAMAM_REPEATED_ACTION_TOTAL_LIMIT` |

A watchdog task also checks every second, so a hung device call cannot outlive its budget.
A tripped guardrail is an **error**, never a test failure: the app was not shown to be broken.

## 3. Verify (`harness/verification.py`, `harness/verdict.py`)

Verification is ordered so that code decides first:

1. **Infra error → `ERROR`.** A run that could not execute is never reported as a failing test.
2. **Deterministic checks.** User-declared checks (`text_present`, `element_exists`, …) plus
   `action_used` checks the harness derives from the test text: a test that says "background
   the app" cannot pass unless the step log shows the app was backgrounded
   (`harness/checks.py: required_actions`). Any failed check → `FAILED`, citing the rule.
3. **All checks passed and the test declares them exhaustive → `PASSED`**, no model consulted
   — and the agent's own verdict and checklist are not consulted either. Use
   `checks_exhaustive` only when your checks fully define the test.
4. **Otherwise**, one of:
   - **Agent verdict (default).** The agent's own conclusion decides — but the agent could only
     reach a PASS through the evidence-gated stop gate (see
     [Verdicts and trust](verdicts-and-trust.md)). A run that ended without the agent
     concluding is `FAILED` if a checklist item was already seen to fail, otherwise `ERROR`
     ("inconclusive") — never a guessed pass.
   - **Judge (optional, `TAMAM_JUDGE_ENABLED`).** A separate model grades the evidence bundle
     without seeing the agent's opinion. If the two disagree the run is flagged `disputed`.

## 4. Persist (`runtime/session.py: _persist`, `_graduate`)

The outcome, reason, evidence (steps, screenshots, final screen, device logs, video), token
usage and timings are stored. Persisting is shielded from cancellation, so a worker shutdown
mid-teardown cannot leave a run stuck in a non-terminal state; a reaper
(`reap_stale_runs`) marks any run that was orphaned anyway.

After a passing iOS run, the harness tries to **graduate** it — compile the run into a
replayable script. That never changes the verdict; it only affects future runs.

## Live view

While a run executes, step events stream to the UI over server-sent events
(`runtime/events.py`), and executors that support it publish a live screen preview only while
someone is watching.
