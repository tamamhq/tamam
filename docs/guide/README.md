# Tamam — Guide

Tamam tests mobile apps from plain-English test cases. You write what a person would
check ("Add the Wireless Mouse and the USB Cable to the cart, open Cart, and verify the total
is $35"). An AI
agent carries it out on a real iOS simulator or device, and the harness decides — in code
wherever it can — whether the test passed.

Once a test passes, the harness can **graduate** it: record what the agent did and replay it
deterministically, in seconds, with the AI kept as a safety net for when the app changes.

This guide explains every feature and how it works. It is written for someone who has never
seen the code.

## The mental model in five sentences

1. **The harness owns the outer loop; the model only proposes.** The agent suggests one action
   at a time. The harness executes it, observes the result, and decides when the run is over.
2. **Code before prompt.** Anything that can be checked by a rule is checked by a rule: zero
   tokens, no hallucination. A model is used only for what rules cannot judge.
3. **Nobody trusts the agent's word.** Before a PASS is accepted, the agent must cite
   evidence for every part of the test, and that evidence must be text the screen actually
   showed. A PASS that isn't backed that way ends *inconclusive*, never passed.
4. **Fail closed.** When the harness is unsure — an ambiguous button, a screen it doesn't
   recognise, an unproven claim — it never guesses a PASS. It escalates or reports
   "inconclusive".
5. **Measured, not hoped.** Every behaviour has unit tests *and* an end-to-end eval on a
   fixture app with planted bugs, and a regression script compares every change with the
   previous results before it merges.

## Map of this guide

| Page | Read it to learn |
|---|---|
| [How a run works](how-a-run-works.md) | A test's journey from text to verdict |
| [The iOS agent](ios-agent.md) | How the agent sees the screen, acts, and stays on track |
| [Verdicts and trust](verdicts-and-trust.md) | Why a PASS can be believed: checklists, evidence, gates |
| [Test graduation](test-graduation.md) | Fast deterministic replay with an AI safety net |
| [Devices](devices.md) | Simulator, physical devices, and cloud device farms |
| [Security and privacy](security-and-privacy.md) | Secrets, credentials, risky actions, what is stored |
| [Evals and the regression gate](evals-and-regression-gate.md) | How we prove it works and keep it working |
| [Configuration](configuration.md) | Every setting, and project/test-level options |
| [Extending](extending.md) | Adding an executor, a hook, a check, an eval scenario |

Setup: [the README](../../README.md) and [setup](../setup.md).

## Repository layout (backend)

```
backend/tamam/
  api/routes.py          HTTP API (projects, tests, runs, builds, scripts)
  runtime/session.py     loads a run from the DB, wires collaborators, persists the result
  harness/               the core: HarnessRun, guardrails, checks, judge, verdict resolution
  executors/             things that carry a test out (the iOS agent; web and Android are coming)
    ios/                 the iOS agent: perception, brain, hooks, devices, redaction
  graduation/            compile a passing run into a script; replay it; lifecycle
  evals/                 the eval suite: scenarios, runner, metrics, graduation scoring
backend/scripts/         eval runners and the regression gate
fixtures/ios/            "Shoply", the fixture app with planted bugs used by every eval
```
