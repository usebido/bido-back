"""Alembic environment (async, asyncpg).

Migrations always run as the migration role (MIGRATION_DATABASE_URL), which
owns the schema. They never fall back to DATABASE_URL: the app role cannot
CREATE and must stay that way.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from bido_back.db.models import Base
from bido_back.settings import get_settings

if context.config.config_file_name is not None:
    fileConfig(context.config.config_file_name)

target_metadata = Base.metadata


def _migration_url() -> str:
    url = get_settings().migration_database_url
    if url is None:
        raise SystemExit(
            "MIGRATION_DATABASE_URL is not set. Migrations run as the migration role "
            "(schema owner), never as the app role. Set it in the environment or .env, "
            "e.g. postgresql+asyncpg://bido_migrator:migrator@localhost:54320/bido"
        )
    return str(url)


def run_migrations_offline() -> None:
    context.configure(
        url=_migration_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_migration_url(), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_do_run_migrations)
            await connection.commit()
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
