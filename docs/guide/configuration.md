# Configuration

All server configuration is environment variables with the `TAMAM_` prefix, read by
`backend/tamam/config.py` (a `.env` file in `backend/` works too). Model ids, limits and
backends are settings, never literals in code. Defaults are chosen to be safe: things that
act on your behalf (auto-accepting the app's dialogs, replaying scripts, a judge) start
**off**. (iOS system permission prompts are accepted by default — see
[Security](security-and-privacy.md).)

## Essentials

| Variable | Default | Purpose |
|---|---|---|
| `TAMAM_ANTHROPIC_API_KEY` | — | **required**: the agent, planner and AI looks (bridged to `ANTHROPIC_API_KEY`) |
| `TAMAM_DATABASE_URL` | local SQLite | Postgres in production |
| `TAMAM_REDIS_URL` | unset | set → runs execute on an arq worker; unset → inline in the API process (development only: a restart loses running runs) |
| `TAMAM_WORKER_MAX_JOBS` | `3` | concurrent runs per worker |
| `TAMAM_LMNR_PROJECT_API_KEY` | unset | Laminar tracing (one trace per run); a no-op without it |
| `TAMAM_LMNR_TRACE_CONTENT` | `true` | record LLM prompt text in Laminar traces; `false` keeps test secrets out of Laminar |

## Models

| Variable | Default | Purpose |
|---|---|---|
| `TAMAM_AGENT_MODEL` | `anthropic:claude-sonnet-5` | the iOS agent's brain, the clause planner, AI looks |
| `TAMAM_JUDGE_MODEL` | `anthropic:claude-sonnet-5` | the optional judge |
| `TAMAM_JUDGE_ENABLED` | `false` | consult a judge after the checks (see [Verdicts](verdicts-and-trust.md)) |
| `TAMAM_PROMPT_CACHE` | `true` | prompt caching for the agent |
| `TAMAM_AUTO_PLAN` | `false` | extract extra deterministic checks from the test text (off: it produced false failures) |

## Run budgets and guardrails

| Variable | Default | Purpose |
|---|---|---|
| `TAMAM_DEFAULT_MAX_STEPS` | `40` | step budget per run |
| `TAMAM_DEFAULT_MAX_WALL_CLOCK_S` | `900` | time budget per run |
| `TAMAM_STUCK_TIMEOUT_S` / `TAMAM_STARTUP_TIMEOUT_S` | `120` / `300` | no step event for this long → stop |
| `TAMAM_NO_PROGRESS_LIMIT` | `4` | consecutive actions with no screen change |
| `TAMAM_REPEATED_ACTION_LIMIT` / `_TOTAL_LIMIT` | `6` / `8` | identical actions in a row / in total |
| `TAMAM_DECIDER_TIMEOUT_S` | `60` | one model decision |
| `TAMAM_DRIVER_COMMAND_TIMEOUT_S` / `TAMAM_DRIVER_READ_TIMEOUT_S` | `30` / `60` | device calls |
| `TAMAM_MAX_SESSION_RECONNECTS` | `2` | device-session reconnects per run |
| `TAMAM_JUDGE_TIMEOUT_S` | `120` | the judge call |

## iOS perception and devices

| Variable | Default | Purpose |
|---|---|---|
| `TAMAM_IOS_BACKEND` | `simulator` | `simulator`, `device` or `cloud` ([Devices](devices.md)) |
| `TAMAM_APPIUM_SERVER_URL` | `http://127.0.0.1:4723` | Appium server for simulator/device |
| `TAMAM_IOS_DEVICE_UDID`, `TAMAM_IOS_TEAM_ID`, `TAMAM_WDA_BUNDLE_ID` | — | physical device + WebDriverAgent signing |
| `TAMAM_IOS_CLOUD` | `lambdatest` | `lambdatest`, `browserstack`, `sauce`, `aws_device_farm` |
| `TAMAM_CLOUD_USERNAME`, `TAMAM_CLOUD_ACCESS_KEY`, `TAMAM_SAUCE_REGION` | — | subscription clouds |
| `TAMAM_AWS_ACCESS_KEY_ID`, `TAMAM_AWS_SECRET_ACCESS_KEY`, `TAMAM_AWS_REGION`, `TAMAM_AWS_DEVICEFARM_PROJECT_ARN`, `TAMAM_AWS_DEVICEFARM_APPIUM_VERSION`, `TAMAM_AWS_DEVICEFARM_MIN_IOS` | `us-west-2`, `3`, `26.0` | AWS Device Farm |
| `TAMAM_IOS_PLATFORM_VERSION` | — | default iOS version where a cloud requires one |
| `TAMAM_IOS_SIM_LAUNCH_ARGS` | `[]` | launch arguments for the app on the simulator |
| `TAMAM_IOS_SNAPSHOT_MAX_DEPTH` / `_MAX_CHILDREN` | `40` / `100` | accessibility snapshot caps |
| `TAMAM_IOS_WAIT_FOR_IDLE_S` | `1.0` | XCUITest idle wait per query (farm devices) |
| `TAMAM_IOS_WAIT_FOR_IDLE_LOCAL_S` | `0.2` | The same wait on a local simulator (capped by the one above) |
| `TAMAM_IOS_GROUNDER` | `ocr` | OCR (Tesseract, no model) on vision steps: text the accessibility tree misses becomes tappable and citable evidence; `null` turns it off. Needs the `tesseract` binary (in the Docker image) |
| `TAMAM_IOS_AUTO_ACCEPT_ALERTS` | `false` | auto-accept the app's own dialogs (not recommended) |

## Test graduation

| Variable | Default | Purpose |
|---|---|---|
| `TAMAM_GRADUATION_REPLAY` | `false` | replay graduated scripts (off: compile and show only) |
| `TAMAM_GRADUATION_PROMOTE_AFTER` | `5` | agreeing probation runs before a script runs on its own |
| `TAMAM_SCHEDULED_TESTS` | empty | comma-separated test ids the worker runs once a day ; a test with a run in flight is skipped |
| `TAMAM_SCHEDULED_HOUR_UTC` | `6` | the hour (UTC) of the daily scheduled runs |

## Storage and auth

| Variable | Default | Purpose |
|---|---|---|
| `TAMAM_STORAGE_BACKEND` | `local` | `local` or `s3` (`TAMAM_S3_ENDPOINT_URL`, `TAMAM_S3_BUCKET`, `TAMAM_S3_REGION`, `TAMAM_S3_ACCESS_KEY_ID`, `TAMAM_S3_SECRET_ACCESS_KEY`) |
| `TAMAM_SIGNED_URL_TTL_S` | `3600` | presigned artifact URL lifetime |
| `TAMAM_MAX_UPLOAD_MB` | `500` | build upload cap |
| `TAMAM_ARTIFACTS_DIR` | `./artifacts` | local artifact directory |
| `TAMAM_CLERK_ISSUER`, `TAMAM_CLERK_JWKS_URL`, `TAMAM_CLERK_AUTHORIZED_PARTY` | — | authentication; with no issuer the API denies everything |
| `TAMAM_AUTH_DEV_MODE`, `TAMAM_AUTH_DEV_ORG_ID` | `false`, `org_dev` | local development only |
| `TAMAM_CORS_ALLOW_ORIGINS` | `[]` | allowed browser origins |

## Project and test settings (API)

Some behaviour is configured per project or per test rather than per server. A test-level
value overrides the project's; `null` means "inherit".

| Setting | Values | Where |
|---|---|---|
| `graduation.fast_mode` | `auto` (default), `off` | `PATCH /api/tests/{id}`; project default in `Project.settings.graduation` (no API yet) |
| `graduation.allow_risky_actions` | `false` (default), `true` | same |

Per test you also set the executor (`ios_agent`), the app build, device model, iOS version,
`ios_target`, `reuse_session`, and optional deterministic checks (`text_present`,
`text_absent`, `element_exists`, …), with `checks_exhaustive` to let checks alone decide a PASS.
The step and time budgets are set per run (`max_steps`, `max_wall_clock_s` on the run request).
