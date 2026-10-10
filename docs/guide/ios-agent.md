# The iOS agent

The iOS agent (`executors/ios/executor.py: IOSAgentExecutor`) carries a test out on an iOS
simulator or device. It is a loop:

```
observe the screen ─► (show a screenshot if needed) ─► model decides one action ─► act ─┐
      ▲                                                                                  │
      └──────────────────────────────── until the model says "done" or a budget ends ◄──┘
```

Every collaborator — device driver, perceiver, vision policy, model ("decider"), hooks — is
injected, so the whole loop is unit-tested with fakes: no device, no Appium, no API key.

## Seeing the screen: accessibility first, vision as fallback

**Accessibility tree (every step).** The perceiver (`executors/ios/perceiver.py`) reads the
XCUITest accessibility hierarchy that Appium/WebDriverAgent exposes and turns it into an
*action space*: a numbered list of on-screen elements, each with a type, label, frame and
state (`[e12] Button "Add to Cart"`). Rules that matter:

- **Viewport only.** Only what is on screen is listed, so the agent must scroll to discover —
  as a person would. A whole-tree walk also makes some apps load data nobody scrolled to.
- **Noise removed.** Keyboard keys, decorative duplicates and off-screen nodes are dropped;
  the snapshot depth and children are capped (`TAMAM_IOS_SNAPSHOT_MAX_DEPTH`,
  `TAMAM_IOS_SNAPSHOT_MAX_CHILDREN`).
- **State is read from the app.** A switch's on/off, a selected tab or chip, an alert's title
  — all come from the tree, never from what a screenshot seems to show.

**Vision (when needed).** `VisionEscalation` (`executors/ios/hooks.py`) decides when the model
also gets a screenshot: on step 1, when the tree is uninformative (icon-only controls named
like `square.grid.2x2`, see `executors/ios/vision.py`), or when the agent is stuck. The screenshot
is drawn with *set-of-marks*: each element's number over its frame, so the model refers to
what it sees by the same ids it would use from the tree.

**Grounder (optional).** For UI the tree doesn't expose at all, a grounder can find tap
targets in the screenshot and add them to the action space as ordinary elements
(`executors/ios/grounder.py`). The default is `ocr` (Tesseract, no model): it adds text the
tree misses as tappable, citable elements. `TAMAM_IOS_GROUNDER=null` turns it off.

## Acting

The model returns one structured **turn** (`executors/ios/brain.py: Turn`):

- `evaluation_previous` — did the last action do what was intended? (forces it to notice a
  tap that did nothing);
- `memory` — short notes carried forward (values to compare later, what is next);
- `clause_updates` — checklist items it has now *observed* done or failed, each citing the
  step whose screen showed it (see [Verdicts and trust](verdicts-and-trust.md));
- `action` — exactly one of:

| Action | Does |
|---|---|
| `tap(ref)` | tap an element by its number |
| `type(ref, text, clear)` | type into a field (empty ref = the focused field) |
| `swipe(direction, ref)` | scroll; with a ref, swipe that row (e.g. a sideways strip) |
| `back` | the screen's Back control, else the system edge-swipe |
| `wait(seconds)` | let a spinner or load finish (bounded) |
| `tap_point(x, y)` | last resort: tap a screen coordinate seen in the screenshot |
| `background_app(seconds)` | background the app and bring it back |
| `done(passed, reason, repro_steps)` | conclude |

**Device session** (`executors/ios/session.py: DeviceSession`). Observing and acting go
through one shared session object with the recovery rules in one place: a wedged read or a
dead Appium session is reconnected (up to `TAMAM_MAX_SESSION_RECONNECTS` per run); a failed
action becomes an *observation* the agent reads, not a crash; only an unrecoverable session
ends the run. Fast replay uses the same session object.

## Hooks: the harness's hands on the loop

Hooks (`executors/ios/hooks.py`) run at fixed points of each step (before/after observe,
after an action, at `done`). The default pipeline, in order:

| Hook | What it does |
|---|---|
| `AcceptAlerts` | iOS **system** dialogs (permission prompts) are accepted, and the agent is told. The app's **own** dialogs are left to the agent — auto-accepting them once made a "Couldn't save" error vanish unseen |
| `HideKeyboard` | puts the keyboard away before a snapshot (it multiplies the tree ~80×) |
| `LearnIcons` | after tapping an icon-only control, reads what it turned out to be and tells the agent, so it never has to guess that icon again |
| `EffectAnnotation` | tells the agent what its last action *did* (screen changed? what is now selected? did the switch stay on?) |
| `VisionEscalation` | decides whether this step gets a screenshot, and draws it |
| `RepetitionWarning` | after the 3rd identical action, warns before the guardrail ends the run |
| `RequiredActionGate` | refuses a PASS if the test requires an action (e.g. backgrounding) the log doesn't show |
| `BlankVerdictGate` | refuses a verdict formed on an empty screen; look again |
| `ClauseLedger` | the checklist and its evidence gate (see next page) |

A gate that refuses a verdict does so a bounded number of times, so it can never block a run
forever; the refusal text becomes the agent's next context.

## Evidence the run produces

Each step emits a `StepEvent` (action, screen signature, screenshot paths) and a compact
**step trace** (screen texts, anchors, the tapped target's type/label/position, switch and
selection state). The trace is what [test graduation](test-graduation.md) compiles. At the
end the executor returns an evidence bundle: steps, final screen text and screenshot,
device and crash logs, the checklist, the agent's verdict, token usage, and a video.
