# Verdicts and trust

An AI agent that says "PASS" is easy to build. One whose PASS you can believe is the point of
this project. The rule throughout: **the harness decides what "done" means, in code, and the
agent must show evidence the harness can check.**

## The checklist (clause ledger)

Before the first step, the test text is split into ordered **clauses**
(`executors/ios/plan.py`): "Open the More tab", "Tap Orders", "Verify the Orders screen shows
'No orders yet'". A model planner does the split; a deterministic splitter is the fallback.
The checklist is *seeded from the test*, so the agent cannot quietly shrink it. It holds at
most 8 clauses (`MAX_CLAUSES`); in a longer test the extra parts are folded into the last
clause — never dropped — so each still needs evidence. Keep tests to one flow anyway.

Every turn the agent sees the checklist with each clause's status, and reports progress as
`clause_updates`: a clause is `done` or `failed`, citing **the step number whose screen showed
it** and a short description of what it saw. A citation of a step that hasn't happened yet
is rejected.

## The stop gate

When the agent says `done(passed=true)`, the ledger checks, in order:

1. **No clause failed.** A PASS that contradicts a failed clause is refused.
2. **Every clause has evidence.** Stopping before covering every clause was the most common
   real-app failure; a PASS with open clauses is refused, naming them.
3. **Verification evidence quotes the page's content.** For every clause that makes a claim
   (anything that doesn't start with an action verb like "Open", "Tap", "Type"), the evidence must name or quote text that was on screen
   at the cited step (±1) — **and not the page's title, navigation bar, tab bar, or a label
   the agent itself tapped**. A page that *opened* is not a page that *shows what the test
   asks*: without this rule the agent once passed a blank Terms of Service page because "the
   screen opened showing its title" (`ClauseLedger.unsupported`).

A refusal is sent back as the agent's next context ("quote the specific on-screen text that
proves it; if the page doesn't show it, mark the clause failed").

**Refusals are bounded, but they cannot be worn out.** So that a gate never blocks a run
forever, the ledger refuses a PASS at most twice (`GATE_REFUSALS`); then the run ends. If the
agent still insists while a clause is failed, open, or backed only by a title, those clauses
are marked and the outcome is **inconclusive** (`ERROR`), never `PASSED`
(`verdict.resolve_without_judge`). In practice a refusal makes the agent look again and it
corrects itself.

**The agent concludes on what it last saw.** Something that appears a moment after its last
look — a dialog, an error toast — can be missed. Measured on the fixture: about 1 run in 20
for a slow error dialog. A re-read of the screen before accepting a PASS is planned.

Other gates in the same family (see [The iOS agent](ios-agent.md)): a PASS is refused when
the test requires an action the step log doesn't show (`RequiredActionGate`, plus the
deterministic `action_used` check), and when the verdict was formed on an empty screen
(`BlankVerdictGate`).

**Fail fast.** Once a clause is marked failed, the agent is told the test has failed and to
conclude — extra steps on a slow app are time spent for nothing.

## Who decides the verdict

Verification order (`harness/verdict.py`):

1. infra error → `ERROR`;
2. any deterministic check failed → `FAILED`;
3. exhaustive checks all passed → `PASSED`;
4. otherwise the **agent's verdict** — which, as above, had to get through the evidence
   gates. If the run ended with no verdict at all: `FAILED` when a clause was
   observed to fail, otherwise `ERROR` ("inconclusive: the agent did not reach a verdict
   (2/3 checklist clauses evidenced)").

**Optional judge.** With `TAMAM_JUDGE_ENABLED=true`, a separate model (`harness/judge.py`)
grades the evidence bundle — step log, journey screenshots, the final settled screen —
without ever seeing the agent's own verdict (no anchoring). It has no tools, by design: a
judge that could re-investigate could be steered by a hostile page. When judge and agent
disagree the run is marked `disputed`. The judge is off by default since the evidence-gated
agent verdict proved more accurate on real apps.

## Outcomes

| Outcome | Means |
|---|---|
| `PASSED` | every deterministic check held, and every checklist clause is done with on-screen evidence (or the test's checks are declared exhaustive) |
| `FAILED` | a check failed, or the agent observed and reported a defect |
| `ERROR` | the run could not decide: infra failure, a guardrail tripped, inconclusive, or **blocked** — the agent could not perform a step (a gesture it lacks, a control it cannot reach, an iOS sheet, missing test data). A blocked run is never reported as an app failure |

`ERROR` is deliberately separate from `FAILED`: "we could not tell" is never reported as
"your app is broken", and never as "your app works".

## What "fail closed" means here

- An ambiguous tap target is never guessed (the agent picks by number; replay refuses).
- A claim without on-screen evidence is refused, and a PASS that still rests on one is inconclusive.
- A run that hits its budget without concluding is inconclusive, not passed.
- In [test graduation](test-graduation.md), anything replay can't confirm hands the test
  back to the full AI run rather than passing it.
