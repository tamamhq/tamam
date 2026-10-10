# Extending

The harness is built from parts behind narrow interfaces. Most extensions are one new class
plus one registration, and none needs an edit in the layers above it. Every extension follows
the change policy: a unit test and an eval on the fixture app
([Evals](evals-and-regression-gate.md)).

## How the parts fit

| Layer | Package | Holds |
|---|---|---|
| Base | `config`, `plugins`, `observability` | settings, the registries, tracing |
| Contract | `executors/base.py`, `executors/core/` | `ExecutionSpec` / `ExecutionResult`, the shared screen model |
| Trust layer | `harness/` | verdicts, checks, clauses, guardrails, judge |
| Platforms and replay | `executors/ios/`, `graduation/` | the iOS agent and devices; fast replay |
| Composition | `assembly.py`, `runtime/`, `evals/`, `api/`, `cli.py` | wiring parts into runs |

A layer imports only from the layers above it in this table; `tests/test_architecture.py`
enforces it, lazy imports included. `assembly.assemble_run()` is the one place a run is put
together (guardrails, verifier, judge, planner), so the dashboard, the CLI and the evals all
run the same harness.

## Registries: replacing or adding a part

Each kind of part has a `Registry` (`tamam/plugins.py`): a name, a factory and a little
metadata. Built-ins register in code; another package adds parts through the entry-point
group `tamam.<kind>` without changing Tamam:

```toml
# your package's pyproject.toml
[project.entry-points."tamam.grounders"]
my_ocr = "my_pkg.grounder:register"
```

```python
def register(registry):
    registry.register("my_ocr", MyGrounder)          # then: TAMAM_IOS_GROUNDER=my_ocr
```

`registry.register(name, factory, replace=True)` swaps a built-in on purpose; re-registering
a name without it is an error.

| Registry | Group | Chosen by |
|---|---|---|
| `executors.registry.EXECUTORS` | `tamam.executors` | the test's `executor_id`, else the one registered `default=True`, else `ios_agent` |
| `executors.ios.providers.DEVICE_PROVIDERS` | `tamam.ios_device_providers` | the test's target, then `TAMAM_IOS_BACKEND` / `TAMAM_IOS_CLOUD` |
| `executors.ios.grounder.GROUNDERS` | `tamam.grounders` | `TAMAM_IOS_GROUNDER` |
| `assembly.JUDGES` | `tamam.judges` | `TAMAM_JUDGE_NAME` (when `TAMAM_JUDGE_ENABLED`) |
| `executors.devices.CATALOGS` | `tamam.device_catalogs` | the platform (device pickers and farm-copy deletion in the API) |

## A new executor or platform

1. Implement the protocol in `executors/base.py`:

   ```python
   class Executor(Protocol):
       executor_id: str
       async def execute(self, spec: ExecutionSpec, on_event: OnEvent,
                         heartbeat: Heartbeat = _noop_heartbeat) -> ExecutionResult: ...
   ```

   Read what is under test from `spec.target`: an `AppTarget` (build, bundle id, device, OS
   version, backend) or a `WebTarget` (start URL); app executors use `spec.app`. Call
   `on_event(StepEvent(...))` once per step (the guardrails count steps from it) and
   `heartbeat()` during long calls. Return **evidence**, never a verdict: the steps, the final
   screen (`final_text`, `final_screenshot_path`), `Diagnostics` (errors, crashes, failed
   requests) and the agent's self-verdict.
2. Optionally implement `attach(services: RunServices)` to receive a live-view sink and a
   `record_upload` callback for apps uploaded to a device farm (the CLI and evals pass none).
3. Register it: `register("android_agent", factory, platform="android", graduates=True)`, or
   from a plugin's `tamam.executors` entry point.
4. A new platform's elements use the shared roles in `executors/core/contracts.py`
   (`ROLES`), so clauses, checks and graduation work unchanged; `tests/test_platform_seams.py`
   shows an Android-style screen compiling and replaying.

For fast replay on the new platform, implement `graduation/platform.py: ReplayPlatform` (open a
device for replay, build the AI agent for a hand-off) and register a `ScriptExecutor` with it.
`executors/ios/replay_device.py` is the iOS example.

## A new device provider

Implement `driver(spec) -> DeviceDriver` and `cleanup()` (see
`executors/ios/providers/simulator.py`), take `services` in the constructor, and report farm
uploads with `record_upload(services, build_id, vendor, ref)` instead of writing to the
database. Register it in `DEVICE_PROVIDERS`. `providers/aws_device_farm.py` is a complete
example of a farm with its own session API. The agent, perceiver and replay never change.

## A new hook (behaviour inside the agent loop)

Subclass `Hook` in `executors/ios/hooks.py` and implement any of:

| Method | Runs | Typical use |
|---|---|---|
| `before_observe(ctx)` | before the screen is read | prepare the device (hide the keyboard) |
| `after_observe(ctx)` | after the read, before the model decides | add context, decide on a screenshot |
| `after_action(ctx, action, note)` | after acting; returns the step's note | tell the agent what happened |
| `on_done(ctx, done)` | when the agent concludes; return a refusal string to reject it | stop gates |

Add it to `default_hooks` (order matters where hooks share state). Keep refusals bounded with
`ctx.refusals[self.name]` so a gate never blocks forever. `StepContext` declares all shared
state in one place.

## A new deterministic check

```python
@check("my_kind", label="…", description="…", placeholder="…")
def _my_kind(value: str, result: ExecutionResult) -> tuple[bool, str]:
    return passed, "evidence shown to the user"
```

in `tamam/harness/checks.py`, and add the kind to the `CheckKind` literal in the same file so API
payloads validate. It then appears in the API's check catalogue and runs before any model is
consulted.

## A new graduation check kind

Add the kind to `graduation/script.py: Check`, teach `graduation/compiler.py` when to lock it
(it must discriminate and use only values the test states), and `graduation/replay.py:
check_holds` how to evaluate it. Bump `SCRIPT_SCHEMA` only if an existing field changes
meaning. Add a replay-eval scenario where the check must catch a planted bug.

## A new eval scenario

1. Plant the defect in the fixture: a new constant in `enum BUG`
   (`fixtures/ios/Sources/ShoplyApp.swift`), correct and buggy values, on its own
   reachable path. Avoid changing screens existing scenarios use — extra rows on a shared
   screen change what older scenarios were measured on. Rebuild with `fixtures/ios/build.sh`.
2. Add the scenario to `SPECS` in `tamam/evals/scenarios.py`: the plain-English objective and the
   oracle (`c_req`/`c_forbid` for the correct build, `b_req`/`b_forbid` for the buggy one).
3. Add its graduation expectation to `EXPECTED` in `tamam/evals/graduation.py` (a unit test
   fails if you forget).
4. Run `scripts/regression.sh` and compare with the previous run.

## A new model

Model ids are settings (`TAMAM_AGENT_MODEL`, `TAMAM_JUDGE_MODEL`); the model layer is pydantic-ai,
so any provider it supports works. Measure a switch with the full regression gate before
adopting it — prompts and models are changed only behind evals.
