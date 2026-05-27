import asyncio
from logging.config import fileConfig

from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from app.config import settings
from app.models import *  # noqa: F401, F403  — register all models
from app.models.base import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def include_name(name: str, type_: str, parent_names: dict[str, str | None]) -> bool:
    """Exclude the 'auth' schema from autogenerate reflection."""
    if type_ == "schema":
        return name != "auth"
    if type_ == "table" and parent_names.get("schema_name") == "auth":
        return False
    return True


def include_object(obj, name, type_, reflected, compare_to) -> bool:  # noqa: ANN001
    """Exclude auth-schema objects from both model and DB sides."""
    if type_ == "table" and getattr(obj, "schema", None) == "auth":
        return False
    return True


def _migration_url() -> str:
    """alembic は migration_database_url (vector role) を優先、なければ database_url。"""
    return settings.migration_database_url or settings.database_url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    context.configure(
        url=_migration_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_name=include_name,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_name=include_name,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connectable = create_async_engine(_migration_url())
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
