# Evals and the regression gate

Unit tests prove each piece does what its author meant. They cannot prove the agent catches
real bugs on a real device. For that, every behaviour is measured end to end on a fixture
app with planted bugs, and a regression script compares each change with the previous
results.

## The fixture: "Shoply" (`fixtures/ios/`)

A small SwiftUI commerce app built twice from one source: a **correct** build where every
flow works and a **buggy** build with planted defects. All defects live in one `enum BUG` in
`fixtures/ios/Sources/ShoplyApp.swift`, each on its own reachable path so one bug
never hides another. Build both with `fixtures/ios/build.sh`.

The defects cover what real apps get wrong: a misspelled brand, a wrong price, a no-op
button, search and filters that don't filter, a dead menu row, a wrong total, a payment that
never confirms, a category missing from a sideways-scrolling bar, a filter lost on resume, reviews that
never load, an infinite list with a wrong price deep down, an error dialog over a "Saved"
screen, a Cancel that signs you out anyway, a switch that snaps back — and four that only a
careful *look* can catch: a blank page, a half-loaded list, the wrong page behind the right
title, and an error banner over real content.

Each scenario (`tamam/evals/scenarios.py`) is a test written in plain English, run on **both**
builds: it must pass on the correct one and fail on the buggy one. An *oracle* also reads the
final screen to confirm the agent really reached it.

## The evals (`backend/scripts/`)

| Script | Answers | Pass bar |
|---|---|---|
| `run_evals.py` | Does the agent pass good builds and catch every planted bug? | accuracy, catch rate, false alarms (`tamam/evals/metrics.py`); `--repeats N` measures flakiness |
| `replay_eval.py` | For each scenario: record an AI run, compile it, replay on both builds. Is the compiled script sensible, and does replay pass the good build and never the buggy one? | **0 false passes**; graduation scored by `tamam/evals/graduation.py: score_run` |
| `handoff_eval.py` | When an active script breaks mid-test, does the AI take over on the same device and still get the right verdict? | passes correct, never passes buggy |
| `stability_eval.py` | Is replay of an **unchanged** app stable? Each scenario is recorded once and its script replayed 5× on the same build (45 replays) | ≥ 95% pass, **0 false app bugs** on the correct build, median replay time ≤ 40% of the AI run's |
| `graduation_eval.py` | Compile-only scoring (standalone; the gate gets it from `replay_eval.py`) | sensible scripts, risky runs blocked |

Metrics treat "caught a bug" as the positive class: a **false pass** (buggy build passed) is
the worst outcome there is; a **false alarm** (good build failed) is the second. Errors are
tracked separately and never counted as either.


## The regression gate (`backend/scripts/regression.sh`)

One command, run before merging any change to the harness. It is a manual step (it needs a
Mac with a simulator, and takes about an hour); CI runs the unit tests and lint on every PR.

```bash
cd backend
scripts/regression.sh                    # everything (~55 min on one simulator)
scripts/regression.sh replay handoff     # only the parts a change touched
scripts/regression.sh --quick            # unit tests + replay
scripts/regression.sh --full             # everything + the stability gate (~25 min more)
```

It runs the unit tests, the agent eval on every scenario, the replay eval (which also scores
graduation), and the hand-off eval; saves every log under
`backend/artifacts/regression/<timestamp>/`; and prints this run's summary **next to the
previous run's**, so a drop in accuracy or speed is visible at a glance — comparing them is the
reviewer's job. It fails outright when:

- any unit test fails;
- replay passes any buggy build (a false pass) or compiles a script judged not sensible;
- the hand-off eval is not all OK;
- the agent passes a buggy build that is **not** listed in `scripts/known_false_passes.txt`.

A known false pass must name its issue (`blank-terms-bug …`) and comes off the list when
fixed — the gate keeps it visible without blocking unrelated work.

## The change policy

Every fix ships with **a unit test and an end-to-end eval on the fixture** — never on a
customer's app, and never with app-specific special cases. When an eval exposes a weakness
that isn't the change's fault, it becomes its own issue rather than a quiet workaround.
