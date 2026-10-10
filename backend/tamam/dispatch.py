"""Run dispatch: ARQ when Redis is configured, inline asyncio task otherwise.

Inline mode keeps the vertical slice runnable with zero infrastructure;
ARQ is the production path.
"""

from __future__ import annotations

import asyncio
import logging

from tamam.config import get_settings

logger = logging.getLogger(__name__)

_arq_pool = None
_inline_tasks: set[asyncio.Task] = set()


async def enqueue_run(run_id: str) -> str:
    settings = get_settings()
    if settings.redis_url:
        global _arq_pool
        if _arq_pool is None:
            from arq import create_pool
            from arq.connections import RedisSettings

            _arq_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        # _job_id dedupes at the queue: a redelivery or accidental double-enqueue of the
        # same run collapses to one job (paired with the QUEUED-only guard in _prepare).
        await _arq_pool.enqueue_job("execute_run", run_id, _job_id=f"run:{run_id}")
        return "arq"

    if not settings.auth_dev_mode:
        # Inline execution turns THIS process into the run executor with no queue,
        # durability, or backpressure — fine for local dev, dangerous in production. If
        # Redis is unset outside dev mode it's almost certainly a misconfiguration.
        logger.warning("TAMAM_REDIS_URL is unset outside dev mode — running %s INLINE "
                       "(no durability/backpressure); set Redis for production", run_id)

    from tamam.runtime.session import run_session

    task = asyncio.create_task(run_session(run_id))
    _inline_tasks.add(task)
    task.add_done_callback(_inline_tasks.discard)
    return "inline"
