import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="TAMAM_", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./tamam.db"
    redis_url: str | None = None  # unset → runs execute inline via asyncio task
    artifacts_dir: Path = Path("./artifacts")
    max_upload_mb: int = 500  # hard cap on build (.ipa) uploads — abuse/DoS guard

    # ── Artifact storage (screenshots, recordings, build .ipa) ──
    # "local": filesystem under artifacts_dir (dev / single box).
    # "s3": any S3-compatible object store (Cloudflare R2, AWS S3) —
    # required once api and worker are separate services (they can't share a disk).
    storage_backend: str = "local"           # "local" | "s3"
    s3_endpoint_url: str | None = None        # e.g. https://<account>.r2.cloudflarestorage.com
    s3_bucket: str | None = None
    s3_region: str = "auto"                   # R2: "auto"; AWS: e.g. "us-east-1"
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    signed_url_ttl_s: int = 3600              # presigned GET URL lifetime (private buckets only)

    # The grader must be at least as capable as the actor — a weaker checker grading a
    # stronger maker is backwards, and Haiku intermittently malforms the structured verdict
    # (see judge.py strict→lenient fallback). Verdict trust > the marginal per-run cost of
    # one Sonnet call. Override to Haiku only where cost dominates and objectives are simple.
    judge_model: str = "anthropic:claude-sonnet-5"
    anthropic_api_key: str | None = None
    lmnr_project_api_key: str | None = None
    # Record LLM prompt/response text in Laminar traces. Off (default): no test secrets (typed
    # passwords, OTPs) can reach Laminar; spans keep timing and tokens. On: full debugging detail.
    lmnr_trace_content: bool = False

    executor_llm_model: str = "claude-haiku-4-5-20251001"  # web plugin (browser-use) only
    # Prompt caching for the brain: the action schema + system prompt (+ objective) are
    # identical on every step of a run, so they are cached and re-read at 0.1x price.
    prompt_cache: bool = True
    # The LLM judge re-grades every run from a sample of screenshots. Off by default: measured
    # on real apps and on the Shoply evals, it overruled correct verdicts and never fixed a
    # wrong one, because it cannot see the middle-of-run screen where the evidence usually is.
    # The agent's evidence-gated verdict decides instead.
    judge_enabled: bool = False
    judge_name: str = "anthropic"    # which registered judge (tamam.assembly.JUDGES)
    agent_model: str = "anthropic:claude-sonnet-5"  # agent brain — Sonnet: complex real-app
    # navigation floundered on Haiku (invented actions, got lost). Override via TAMAM_AGENT_MODEL.
    cdp_url: str | None = None  # web plugin only: drive a remote browser over CDP
    appium_server_url: str = "http://127.0.0.1:4723"  # iOS executor: Appium server (local sim or farm)
    ios_backend: str = "simulator"          # "simulator" | "device" | "cloud"
    ios_device_udid: str | None = None       # real device attached to this Mac
    # Host ports of the local WebDriverAgent. Two XCUITest sessions on one Mac (e.g. an
    # eval on a second simulator) collide on WDA's defaults (8100 for commands, 9100 for
    # the MJPEG live view); set distinct ones per Appium session. Unset = Appium/WDA
    # defaults (wdaLocalPort is then not sent; MJPEG stays on 9100).
    ios_wda_local_port: int | None = None
    ios_mjpeg_server_port: int | None = None
    # Launch arguments passed to the app under test (Appium processArguments). Lets a
    # target auto-configure itself — e.g. an app's own test-login launch argument, so the
    # agent never types a password. JSON list in the env var.
    ios_sim_launch_args: list[str] = []
    # Grounder for text the accessibility tree can't express (custom, React Native or canvas
    # views — e.g. a populated rich-text editor). "ocr" (default) = deterministic Tesseract OCR,
    # no model: finds on-screen text and gives it precise coordinates, so it becomes tappable
    # AND citable evidence (the model picks WHAT, OCR computes WHERE). Adds only text the tree
    # lacks, and runs only on vision-fallback/stuck steps, so it's free on the accessibility
    # path. "null" turns it off. Needs the `tesseract` binary.
    ios_grounder: str = "ocr"            # "ocr" | "null"
    ios_team_id: str | None = None           # Apple Team ID, for signing WebDriverAgent
    wda_bundle_id: str | None = None         # unique WDA bundle id (e.g. com.you.WDA)
    # Cloud real-device farm — vendor-agnostic (all speak Appium)
    ios_cloud: str = "lambdatest"            # "lambdatest" | "browserstack" | "sauce" | "aws_device_farm"
    cloud_username: str | None = None
    cloud_access_key: str | None = None
    ios_platform_version: str | None = None  # e.g. "16" — some clouds require it
    sauce_region: str = "us-west-1"          # Sauce data center (sauce only)
    # AWS Device Farm (ios_cloud="aws_device_farm"): real devices via a managed Appium
    # endpoint exposed inside a remote-access session. Pay-per-minute ($0.17/device-min,
    # 1000 free) vs the flat vendor subscriptions. Creds are passed EXPLICITLY to the
    # devicefarm boto3 client (TAMAM_-prefixed, not the ambient AWS_* vars) so they can't
    # leak into other AWS calls in the process. Device Farm lives only in us-west-2.
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_region: str = "us-west-2"
    aws_devicefarm_project_arn: str | None = None
    # Appium server major version for Device Farm remote sessions. Device Farm defaults to
    # 2 on iOS < 27, whose WebDriverAgent (9.x) silently DROPS snapshotMaxChildren — the
    # cap that stops a tree walk paging in a whole feed. Measured: on 2 the
    # setting was absent from WDA's reply and trees grew to 4.9 MB (90 s reads); a
    # session on 3 (server 3.1.0) kept snapshotMaxChildren=100.
    aws_devicefarm_appium_version: str = "3"
    aws_devicefarm_min_ios: float = 26.0     # floor for device pick (some apps need 26+)

    # ── Auth (Clerk, behind our own Principal seam) ──
    # Verification is by the token's RS256 signature against Clerk's JWKS — we never
    # trust an unsigned/unverified claim. Secure by default: if no issuer is set, the
    # API denies (401) unless auth_dev_mode is explicitly enabled for LOCAL dev only.
    clerk_issuer: str | None = None          # e.g. https://your-app.clerk.accounts.dev
    clerk_jwks_url: str | None = None        # defaults to {issuer}/.well-known/jwks.json
    clerk_authorized_party: str | None = None  # expected `azp` (your frontend origin)
    auth_dev_mode: bool = False              # LOCAL ONLY: inject a dev principal, no token
    auth_dev_org_id: str = "org_dev"         # the org used in dev mode / for legacy rows
    # Browser origins allowed to call the API cross-origin (the SPA, when served from a
    # different origin than the API). Empty = no CORS headers (same-origin / dev proxy).
    cors_allow_origins: list[str] = []

    worker_max_jobs: int = 3  # concurrent browser runs per ARQ worker process

    # Guardrail defaults (overridable per run spec)
    default_max_steps: int = 40  # authenticated/multi-step flows need headroom (was 25)
    default_max_wall_clock_s: int = 900  # fit ~40 cloud steps (~18s each on BrowserStack); was 300
    # Mid-run idle backstop. With per-command driver + decider timeouts below catching a
    # genuine hang fast, this is a BACKSTOP, not the primary hang-catcher — so it's set
    # above the slowest LEGITIMATE step. Heavy screens on a farm (a modal with the keyboard
    # up → a huge a11y tree → slow page_source + slow typing) can make one step ~50s of
    # real work; 60s false-tripped those. 120s tolerates them while still catching a run
    # where nothing returns.
    stuck_timeout_s: int = 120
    # Grace for session creation + build install + WDA launch, which precede the first
    # step. Real-device farms are slow here: acquire a device + upload/install the .ipa on
    # it + boot WebDriverAgent runs ~150s and varies — 180s tipped over and got runs
    # cancelled mid-provisioning. 300s gives comfortable headroom; wall-clock is the
    # ultimate backstop for a genuinely dead startup.
    startup_timeout_s: int = 300
    # Per-command driver timeout: a single Appium/WDA call (tap, page_source, screenshot)
    # that HANGS must fail so the loop can recover (dismiss a blocking dialog, retry) or
    # end gracefully. Set above real cloud call latency (page_source on a heavy screen can
    # be ~15-20s) so a slow-but-live call isn't force-timed-out into retry churn — only a
    # true hang trips it.
    driver_command_timeout_s: int = 30
    # page_source (the full a11y-tree read) is the one legitimately slow call: on a heavy
    # screen (a modal over a wide feed) serializing the whole tree can take far longer than
    # an action, and it's finite (a human uses the screen). Give the READ its own, larger
    # budget so we don't force-timeout a slow-but-live perception; heartbeats keep the
    # stuck-watchdog correct during it. Taps/swipes keep the tighter command timeout.
    driver_read_timeout_s: int = 60
    # The brain's LLM call is the one other unbounded await in the loop. Real calls are a
    # few seconds; this bounds a wedged API call so it can't stall the loop indefinitely.
    decider_timeout_s: int = 60
    # If the device's Appium/WDA session dies mid-run (app crash, WDA recycle), reconnect
    # to the same session endpoint and continue — but cap it so a reliably-crashing app
    # can't loop forever restarting. After the cap, end gracefully and let the judge grade
    # the partial evidence (a repeated crash is a real finding).
    max_session_reconnects: int = 2
    # The verify/judge phase runs AFTER the guardrailed execute loop, so the guardrails and
    # watchdog no longer apply — a wedged judge call would otherwise hang the run in JUDGING
    # forever. This bounds it; on timeout the run resolves to ERROR (inconclusive), never a
    # false verdict. Judge calls carry several screenshots, so give it generous headroom.
    judge_timeout_s: int = 120
    # XCUITest snapshot tuning (applied at session start via update_settings). page_source
    # serializes the WHOLE app tree AND waits for the app to be "idle" first, so a heavy or
    # animating screen (a modal over a long feed, a keyboard) makes it pathologically slow
    # on a farm (30 s timeouts). These are app-agnostic perception-perf knobs:
    #  - snapshotMaxDepth caps how deep WDA walks (default 50; real interactive elements sit
    #    well within this, so a modest cap slashes serialization cost with no loss in practice)
    #  - waitForIdleTimeout=0 stops WDA blocking on animations/quiescence before returning
    ios_snapshot_max_depth: int = 40
    # Cascade breaker for infinite lists. A full tree walk touches a list's LAST cell,
    # which fires the app's "load more"; the new rows are appended while the walk is still
    # going, so one read can page in dozens of pages (seen on a real infinite feed: several
    # times the visible rows in one step, a 2 MB tree, 60s timeouts). Reading at most N children per
    # container bounds that growth at ~N rows. Measured: 100 drops nothing visible on the
    # feed or on a dense scrolled filter panel; 12-50 DID drop visible rows.
    ios_snapshot_max_children: int = 100
    # Small settle before a11y snapshots so freshly-typed text is reflected (0 caused
    # "field appears empty" fumbles on fast endpoints like AWS Device Farm — BrowserStack's
    # hub latency happened to mask it). 1s is enough to settle without the multi-second
    # quiescence stalls that motivated capping this in the first place.
    ios_wait_for_idle_s: float = 1.0
    # The same wait on a local simulator. Our post-action settle already waits for a
    # still screen there, so XCUITest's own wait mostly double-counts: measured on
    # Shoply, taps 1.42 s → 0.62 s at 0.2 s, 16/16 taps landed, 5/5 typed words read back.
    ios_wait_for_idle_local_s: float = 0.2
    # Legacy switch: WebDriverAgent's autoAcceptAlerts. It polls EVERY active app (the app
    # under test included) for an alert or action sheet every 2s and accepts it — so it
    # silently confirmed the app's own dialogs ("Couldn't save", "Remove all items?") and
    # walked the app's tree in the background. Off: the app's dialogs are left to the agent
    # and iOS system prompts are accepted by the perceiver, reported (see perceiver.py).
    ios_auto_accept_alerts: bool = False
    # Before accepting a PASS, wait this long and read the screen once more: a result (an
    # error dialog) can land just after the agent's last look. 0 turns the check off.
    verdict_settle_s: float = 1.5
    # Test graduation. Scripts are compiled after passing runs regardless; REPLAYING them is
    # off until turned on per environment (probation doubles device sessions and AI runs).
    graduation_replay: bool = False
    # Probation runs (replay + full AI, agreeing) needed before a script runs on its own.
    graduation_promote_after: int = 5
    # Tests to run once a day on the worker (comma-separated test ids), e.g. the fast-mode
    # shadow. Empty = no scheduled runs. Hour is UTC.
    scheduled_tests: str = ""
    scheduled_hour_utc: int = 6
    no_progress_limit: int = 4  # stop after N consecutive no-effect actions (spinning)
    repeated_action_limit: int = 6  # stop after N consecutive identical actions (fixation)
    repeated_action_total_limit: int = 8  # stop after N total occurrences of one action (oscillation)

    # Autonomous deterministic checks: an LLM planner turns the objective into
    # text_present/absent checks. OFF by default — it over-extracts *concept* words
    # from capability objectives ("SSO", "login") into literal string checks that no
    # real UI renders, producing false FAILs that override the judge. Deterministic
    # checks should be user-intentional (the check builder); turn this on only with
    # an eval suite guarding the planner's extraction quality.
    auto_plan: bool = False


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    # pydantic-ai and browser-use read ANTHROPIC_API_KEY from the process env;
    # bridge the .env-provided setting so one file configures everything.
    if settings.anthropic_api_key and not os.environ.get("ANTHROPIC_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
    return settings
