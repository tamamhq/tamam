# Test graduation

An AI run is flexible but slow and costs tokens: every step is a model call. A recorded
script is fast and free but breaks when the app changes. **Graduation gives you both**: a test
that passed with the AI is compiled into a deterministic script, replayed in seconds on
later runs, and the AI stays as a safety net that takes over whenever the script no longer
fits the app.

```
AI run passes ──► compile ──► PROBATION ──(5 agreeing runs, all checks in code)──► ACTIVE
                    │            │ every run: replay + full AI run, compared        │ replay alone
                    │            └─ disagree ──► RETIRED                              │ miss ──► AI takes over
                    └─ refused (reasons shown)                                       └─ 3 misses in a row ──► RETIRED
```

Replay is **off by default** (`TAMAM_GRADUATION_REPLAY=false`): scripts are still compiled and
shown, but every run is a full AI run until you turn replay on.

## 1. Record

During every iOS run the agent writes a **step trace** (`executors/ios/executor.py`): for each
step, the screen's texts and anchors (a few stable texts that identify it), the action, and
the target element — its type, label, accessibility identifier, position, and switch or
selection state. Typed secrets are redacted before anything is stored.

## 2. Compile (`graduation/compiler.py: compile_run`)

The compiler turns a **passing** run's trace and checklist into a `CompiledScript`
(`graduation/script.py`, versioned JSON stored per test in the `testscript` table):

- **Steps.** Taps, typing, scrolls ("scroll until *X* appears"), back, background. The
  agent's `wait`s are not copied: replay polls for the next screen instead of sleeping.
  Each target carries ranked **locators** (accessibility identifier first, then type + label).
  A tap that changed nothing is dropped.
- **Locked checks.** For each verification clause, the compiler tries to lock a check evaluated
  in code: `visible`, `absent`, `toggled` (a switch's state) or `selected` (a chip/tab). It
  refuses on doubt:
  - **only values the test itself states** ("total is $35") — never a value the agent read;
  - **it must discriminate**: not already on screen before the action, and not just the label
    that was tapped;
  - a price is bound to the row the test names ("Golden Ticket — $5"), so another row's $5
    can't satisfy it;
  - an absence ("no error") needs a positive check on the same screen, so a blank screen
    can't pass it.
- **AI looks for the rest.** A clause no rule can check ("the help topics list loads fully")
  is covered by an **AI look** at replay time.
- **Secrets.** A typed credential is stored as a reference to the test text, never as a value.
  A run that typed something invented (an OTP, a made-up email) is refused.
- **Risky actions.** A run that *taps* something that deletes, pays, sends, signs out, etc. is
  refused unless allowed for the project or test — replay would repeat it every run. Detected
  by the button's label, its accessibility identifier (`trashButton`), a generic "OK/Yes" on a
  dialog whose text is risky, or a tap by position on such a screen
  (`compiler.risky_step_reasons`). See [Security and privacy](security-and-privacy.md).

A refused compile records plain-English reasons on the run ("Step 5 taps "Pay", which changes
data or spends money…"). It never affects the run's verdict.

## 3. Replay (`graduation/replay.py: ReplayRunner`)

Replay drives the same `DeviceSession` as the agent, with no model in the loop for steps:

- **Wait for the right screen.** Before each step, the screen's recorded anchors must be
  present (waiting up to 20 s for the app to launch on the first screen).
- **Find the target** (`graduation/resolver.py`): exactly one element, by locator. Several
  matches are **never guessed** (lists reorder) — with one exception: a navigation-bar or
  tab-bar control is told apart by which bar it sits in (a "Shop" back button vs a "Shop" tab).
- **Locked checks run in code.** Absences are evaluated last, after everything else held.
- **AI looks.** For clauses without a code check, a model looks at the replayed screen and
  must answer `holds`, `fails` or `unsure` — and a `holds` must **quote specific text on the
  screen** that isn't the page title or a tapped label. A loading screen is `unsure`; `unsure`
  earns one more look after a pause; anything but a quoted `holds` is a miss.

Replay outcomes: `passed`, `failed` (a locked check failed on the right screen — an app bug),
`miss` (the script no longer fits: target missing or ambiguous, unknown screen, an AI look not
confirmed), or `error`.

## 4. The safety net (`graduation/executor.py: ScriptExecutor`)

- **Probation.** Replay runs, then a full AI run decides the verdict. The two are compared.
- **Active.** Replay alone decides. On a **miss**, the AI **takes over on the same device
  session**, from where replay stopped, with the clauses replay already proved pre-filled —
  no reinstall, no starting over, so a miss costs little more than the replay that preceded it. Action clauses are never pre-marked; only verified ones are.

An AI look never fails a run on its own: a `fails` or `unsure` escalates to the AI.

## 5. Lifecycle (`graduation/lifecycle.py`)

| Event | Result |
|---|---|
| A passing AI run compiles | new **probation** script (replaces an older probation one) |
| Probation: replay and AI agree, **5 in a row** (`TAMAM_GRADUATION_PROMOTE_AFTER`) | **active** — only if every clause is checked in code; scripts that need AI looks stay in probation |
| Probation: replay and AI disagree | **retired** |
| Active: replay misses **3 runs in a row** | **retired** |
| The test's text is edited | retired the next time a replay is considered (the old steps answer a different question) |
| A different build, device target (simulator / physical device / a cloud vendor), device model or iOS version | an active script drops back to probation **there** — screens differ (a fresh farm device shows onboarding, reads are slower, sizes change). It must earn its 5 agreeing runs on that build and device; once promoted there it stays active |
| A script with no record of where it proved itself (compiled before this was tracked) | treated as unproven: probation on its next run |
| Risky actions no longer allowed | the script is not replayed (re-checked before every replay) |
| A hand-off run passes | its run is never compiled into a script (it is half replay, half AI) |

Each script carries a **proof stamp** — the build, target, device and iOS version it proved
itself on — and an **epoch** that increases whenever it must prove itself somewhere new. A
run counts toward promotion only if it ran on the script's current epoch, and the promotion
itself is a conditional update on that epoch, so a run that was already in flight on the old
build or device can never promote the script on the new one. A test that keeps alternating
between devices therefore never reaches "active": that is by design.

## Settings

| Setting | Where | Default |
|---|---|---|
| `TAMAM_GRADUATION_REPLAY` | environment | `false` — compile only, never replay |
| `TAMAM_GRADUATION_PROMOTE_AFTER` | environment | `5` agreeing probation runs |
| `fast_mode` (`auto` / `off`) | test `graduation`, else project `settings.graduation` | `auto` |
| `allow_risky_actions` | test `graduation`, else project `settings.graduation` | `false` |

Set test-level options with `PATCH /api/tests/{id}` `{"graduation": {...}}`; keys are merged,
and `null` removes one (back to inheriting from the project). Project-level values are read
from `Project.settings` but there is no API to set them yet. See the script a test currently
has with `GET /api/tests/{id}/script`.

## Why not a generic script format?

Graduation stores its own small, versioned script format so it can carry what generic test
scripts don't: which clause each check proves, the screen anchors that tell "wrong screen"
from "app bug", locked values traced to the test text, and secret references.
