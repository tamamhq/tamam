import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlmodel import SQLModel

from tamam import models  # noqa: F401 — imports register every table on SQLModel.metadata
from tamam.config import get_settings
from tamam.db import normalize_db_url

config = context.config
if config.config_file_name is not None:
    # disable_existing_loggers=False is critical: migrations run inside app
    # startup, and the default (True) silently disables uvicorn.error — the
    # logger that prints request-time 500 tracebacks to the terminal.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Same async-driver normalization as the app engine, so migrations connect with
# asyncpg whether the URL came from a managed provider (postgresql://) or local.
config.set_main_option("sqlalchemy.url", normalize_db_url(get_settings().database_url))
target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # SQLite can't ALTER in place; batch mode rebuilds tables
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with engine.connect() as connection:
        await connection.run_sync(_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
