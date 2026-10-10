"""Migrations: upgrade head builds the full schema and is idempotent."""

from sqlalchemy import inspect

import tamam.db as db_module
from tamam.db import normalize_db_url


def test_normalize_db_url_rewrites_managed_provider_schemes():
    # Heroku-style providers hand out sync schemes → must drive asyncpg.
    assert normalize_db_url("postgres://u:p@host:5432/db") == "postgresql+asyncpg://u:p@host:5432/db"
    assert normalize_db_url("postgresql://u:p@host/db") == "postgresql+asyncpg://u:p@host/db"
    # Already-async and non-postgres URLs pass through untouched.
    assert normalize_db_url("postgresql+asyncpg://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"
    assert normalize_db_url("sqlite+aiosqlite:///./x.db") == "sqlite+aiosqlite:///./x.db"


async def test_migrate_head_creates_schema_and_is_idempotent(tmp_path, monkeypatch):
    from tamam.config import get_settings

    monkeypatch.setenv("TAMAM_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/migrated.db")
    get_settings.cache_clear()
    await db_module.dispose_db()

    await db_module.migrate_db()
    await db_module.migrate_db()  # second run must be a no-op, not an error

    engine = db_module.get_engine()
    async with engine.connect() as conn:
        tables = await conn.run_sync(lambda sync: inspect(sync).get_table_names())

    assert {"project", "test", "testrun", "runstepevent", "testscript", "alembic_version"} <= set(tables)
    await db_module.dispose_db()
    get_settings.cache_clear()
