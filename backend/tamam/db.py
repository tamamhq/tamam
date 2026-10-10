from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from tamam.config import get_settings

_engine = None
_session_maker: async_sessionmaker | None = None


def normalize_db_url(url: str) -> str:
    """Managed providers (Heroku, Neon…) hand out a sync ``postgres://`` /
    ``postgresql://`` URL, but our engine is async and drives asyncpg. Rewrite the
    scheme so the same connection string works whether it came from a provider or a
    local ``+asyncpg`` URL. SQLite/other URLs pass through untouched."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    return url


def get_engine():
    global _engine, _session_maker
    if _engine is None:
        # pool_pre_ping: validate a pooled connection before use so a stale one (killed by
        # a DB failover / idle timeout) is transparently replaced instead of erroring the
        # first query. Harmless for SQLite. Cheap insurance against a class of prod flakes.
        _engine = create_async_engine(
            normalize_db_url(get_settings().database_url), echo=False, pool_pre_ping=True)
        _session_maker = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
    return _engine


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    get_engine()
    assert _session_maker is not None
    async with _session_maker() as session:
        yield session


async def init_db() -> None:
    """Create tables directly from metadata — tests and throwaway envs only.

    Production/dev startup uses migrate_db(); create_all can't evolve a schema.
    """
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)


def _alembic_upgrade() -> None:
    from alembic import command
    from alembic.config import Config

    backend_dir = Path(__file__).resolve().parent.parent
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "migrations"))
    command.upgrade(config, "head")


# Fixed key for the Postgres advisory lock that serializes migrators.
_MIGRATION_LOCK_KEY = 0x5148_4D49  # "QHMI" (Tamam MIgrations)


async def migrate_db() -> None:
    """Run pending Alembic migrations (Django's `migrate` equivalent).

    Alembic's async env calls asyncio.run(), so it must not run on our loop —
    a worker thread gives it a fresh one.

    On Postgres, hold a session-level ADVISORY LOCK across the upgrade so that N API/worker
    replicas coming up during a rolling deploy don't race `upgrade head` concurrently
    (duplicate-DDL / partially-applied schema). Losers block on the lock, then find nothing
    to do. SQLite is a single local file with no concurrent migrators, so it skips the lock.
    """
    url = normalize_db_url(get_settings().database_url)
    if not url.startswith("postgresql"):
        await asyncio.to_thread(_alembic_upgrade)
        return

    import asyncpg

    dsn = url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", _MIGRATION_LOCK_KEY)
        await asyncio.to_thread(_alembic_upgrade)
    finally:
        try:
            await conn.execute("SELECT pg_advisory_unlock($1)", _MIGRATION_LOCK_KEY)
        finally:
            await conn.close()


async def dispose_db() -> None:
    global _engine, _session_maker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_maker = None
