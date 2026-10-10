"""ARQ worker entry: `arq tamam.worker.WorkerSettings`"""

from __future__ import annotations

from arq import cron
from arq.connections import RedisSettings

from tamam.config import get_settings
from tamam.db import migrate_db
from tamam.runtime.session import run_session


async def execute_run(ctx: dict, run_id: str) -> None:
    await run_session(run_id)


async def run_scheduled_tests(ctx: dict) -> None:
    from tamam.runtime.scheduled import start_scheduled_runs

    await start_scheduled_runs()


async def startup(ctx: dict) -> None:
    from tamam.observability import init_observability

    init_observability("tamam-worker")
    await migrate_db()


class WorkerSettings:
    functions = [execute_run]
    on_startup = startup
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url or "redis://localhost:6379")
    max_jobs = get_settings().worker_max_jobs  # concurrent browser runs per worker
    job_timeout = 900
    # Once a day; a no-op unless TAMAM_SCHEDULED_TESTS lists test ids.
    cron_jobs = [cron(run_scheduled_tests, hour=get_settings().scheduled_hour_utc, minute=0,
                      run_at_startup=False)]
