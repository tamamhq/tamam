"""SQLModel tables.

Design decisions vs. the legacy Django app:
- lifecycle (queued→…→finished) is separate from outcome (passed/failed/error/disputed);
  outcome=error means infrastructure failure, outcome=failed means the test failed.
- Step events are an append-only table, not a JSON blob rewritten every step.
- TestRun has no user_id; ownership derives from test → project.
- The run stores a frozen spec_snapshot for audit.
"""

from __future__ import annotations


import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, UniqueConstraint
from sqlmodel import JSON, Column, Field, SQLModel

from tamam.harness.status import Lifecycle, Outcome  # noqa: F401  (column types; imported from here too)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _tz_col(nullable: bool = False) -> Column:
    """timestamptz on Postgres. We store aware UTC datetimes; naive columns made
    asyncpg reject every insert (SQLite silently tolerated the mismatch)."""
    return Column(DateTime(timezone=True), nullable=nullable)


class Project(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: str = Field(index=True)  # tenant owner — every query scopes to this
    name: str
    description: str = ""
    base_url: str = ""
    # Project-wide defaults, e.g. {"graduation": {"fast_mode": "auto" | "off"}}.
    settings: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_utcnow, sa_column=_tz_col())
    updated_at: datetime = Field(default_factory=_utcnow, sa_column=_tz_col())


class Build(SQLModel, table=True):
    """An app build under a Project — the .ipa/.app, registered/uploaded ONCE and
    reused by many tests. Vendor upload references are cached here so a cloud run
    never re-uploads an unchanged build."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)
    name: str
    app_path: str                      # local .ipa/.app path or a vendor/URL reference
    bundle_id: str | None = None       # app bundle id (for launching an installed app)
    vendor_refs: dict = Field(default_factory=dict, sa_column=Column(JSON))  # {vendor: app_url}
    created_at: datetime = Field(default_factory=_utcnow, sa_column=_tz_col())


class Test(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)
    name: str
    instructions: str
    start_url: str = ""  # web target; unused for mobile
    checks: list = Field(default_factory=list, sa_column=Column(JSON))
    checks_exhaustive: bool = False
    executor_id: str | None = None  # explicit override; None → router decides
    # Mobile target (used by the ios_agent executor; null for web tests)
    build_id: uuid.UUID | None = Field(default=None, foreign_key="build.id")  # the app under test
    app_build_ref: str | None = None   # legacy/override path — build_id is preferred
    app_bundle_id: str | None = None
    device: str | None = None          # e.g. "iPhone 15"
    ios_platform_version: str | None = None  # cloud OS version, e.g. "26" — null → server default
    ios_target: str | None = None      # "simulator" | "cloud" | "device" — null → server default
    # Reuse the app's state (login/keychain) across runs. Default False → each run
    # starts clean (fresh install, keychain wiped) for isolation + so no authenticated
    # session lingers on shared cloud devices. True → persist ("log in once, test many").
    reuse_session: bool = False
    # Per-test override of the project's graduation settings; None = inherit.
    graduation: dict | None = Field(default=None, sa_column=Column(JSON))
    script: dict | None = Field(default=None, sa_column=Column(JSON))  # P2 graduation
    source: str = "manual"
    created_at: datetime = Field(default_factory=_utcnow, sa_column=_tz_col())
    updated_at: datetime = Field(default_factory=_utcnow, sa_column=_tz_col())


class TestRun(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    test_id: uuid.UUID = Field(foreign_key="test.id", index=True)
    spec_snapshot: dict = Field(default_factory=dict, sa_column=Column(JSON))
    lifecycle: Lifecycle = Field(default=Lifecycle.QUEUED, index=True)
    outcome: Outcome | None = Field(default=None, index=True)
    status_message: str = ""
    judge_verdict: dict | None = Field(default=None, sa_column=Column(JSON))
    self_verdict: dict | None = Field(default=None, sa_column=Column(JSON))
    checks_result: dict | None = Field(default=None, sa_column=Column(JSON))
    disputed: bool = Field(default=False, index=True)
    video_url: str | None = None
    # The agent's clause checklist at the end of the run (id, text, status, evidence step),
    # and the storage KEY of its per-step trace (typed text redacted). Both outlive the
    # worker's disk, which a deploy wipes: they are what test graduation compiles from.
    clauses: list | None = Field(default=None, sa_column=Column(JSON))
    trace_key: str | None = None
    # Whether this run could become the test's fast replay, and why not (shown to the user).
    graduation: dict | None = Field(default=None, sa_column=Column(JSON))
    live_url: str | None = None
    usage: dict = Field(default_factory=dict, sa_column=Column(JSON))
    executor_id: str = "ios_agent"  # set when the run starts (runtime/session.py)
    created_at: datetime = Field(default_factory=_utcnow, sa_column=_tz_col())
    started_at: datetime | None = Field(default=None, sa_column=_tz_col(nullable=True))
    finished_at: datetime | None = Field(default=None, sa_column=_tz_col(nullable=True))


class RunStepEvent(SQLModel, table=True):
    """Append-only step log; the UI reads it directly and SSE tails it."""

    id: int | None = Field(default=None, primary_key=True)
    run_id: uuid.UUID = Field(foreign_key="testrun.id", index=True)
    step: int
    payload: dict = Field(default_factory=dict, sa_column=Column(JSON))
    screenshot_url: str | None = None          # raw screen (opaque storage KEY)
    annotated_url: str | None = None           # set-of-marks image the model saw (KEY)
    created_at: datetime = Field(default_factory=_utcnow, sa_column=_tz_col())


class TestScript(SQLModel, table=True):
    """A test's compiled replay (see tamam/graduation). Versions accumulate; at most one is
    the test's current script. Status: probation (new, not yet trusted) | active |
    superseded | retired."""

    __tablename__ = "testscript"
    __table_args__ = (UniqueConstraint("test_id", "version", name="uq_testscript_version"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    test_id: uuid.UUID = Field(foreign_key="test.id", index=True)
    version: int
    status: str = "probation"
    body: dict = Field(default_factory=dict, sa_column=Column(JSON))
    test_text_hash: str
    source_run_id: uuid.UUID | None = None
    # How the script has fared: {"agree": n, "passes": n, "misses": n, "last_mode": ...}.
    stats: dict = Field(default_factory=dict, sa_column=Column(JSON))
    # Bumped each time the script must re-prove itself somewhere new (build/target/device/OS).
    # A run counts toward promotion only if it ran on the script's current epoch.
    epoch: int = 0
    created_at: datetime = Field(default_factory=_utcnow, sa_column=_tz_col())
