"""PostgreSQL session advisory lockの実DB契約。"""

from __future__ import annotations

from contextlib import AbstractContextManager

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.migration_lock import _MIGRATION_LOCK_KEY, migration_advisory_lock

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_migration_lock_survives_transactions_until_same_session_releases(
    test_database_url: str,
) -> None:
    engine = create_async_engine(test_database_url, poolclass=NullPool)
    manager: AbstractContextManager[None] | None = None
    try:
        async with engine.connect() as owner, engine.connect() as contender:

            def acquire(sync_connection: object) -> None:
                nonlocal manager
                manager = migration_advisory_lock(sync_connection)
                manager.__enter__()

            await owner.run_sync(acquire)
            first_conflict = await contender.execute(
                text("SELECT pg_try_advisory_lock(:key)"),
                {"key": _MIGRATION_LOCK_KEY},
            )
            assert first_conflict.scalar_one() is False
            await contender.commit()

            await owner.execute(text("SELECT 1"))
            await owner.commit()
            second_conflict = await contender.execute(
                text("SELECT pg_try_advisory_lock(:key)"),
                {"key": _MIGRATION_LOCK_KEY},
            )
            assert second_conflict.scalar_one() is False
            await contender.commit()

            assert manager is not None
            await owner.run_sync(lambda _connection: manager.__exit__(None, None, None))
            acquired_after_release = await contender.execute(
                text("SELECT pg_try_advisory_lock(:key)"),
                {"key": _MIGRATION_LOCK_KEY},
            )
            assert acquired_after_release.scalar_one() is True
            await contender.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": _MIGRATION_LOCK_KEY},
            )
            await contender.commit()
    finally:
        await engine.dispose()


async def test_nested_migration_lock_remains_held_until_outer_release(
    test_database_url: str,
) -> None:
    engine = create_async_engine(test_database_url, poolclass=NullPool)
    outer: AbstractContextManager[None] | None = None
    inner: AbstractContextManager[None] | None = None
    try:
        async with engine.connect() as owner, engine.connect() as contender:

            def acquire_nested(sync_connection: object) -> None:
                nonlocal outer, inner
                outer = migration_advisory_lock(sync_connection)
                outer.__enter__()
                inner = migration_advisory_lock(sync_connection)
                inner.__enter__()

            await owner.run_sync(acquire_nested)
            assert inner is not None
            await owner.run_sync(lambda _connection: inner.__exit__(None, None, None))

            blocked_after_inner_release = await contender.execute(
                text("SELECT pg_try_advisory_lock(:key)"),
                {"key": _MIGRATION_LOCK_KEY},
            )
            assert blocked_after_inner_release.scalar_one() is False
            await contender.commit()

            assert outer is not None
            await owner.run_sync(lambda _connection: outer.__exit__(None, None, None))
            acquired_after_outer_release = await contender.execute(
                text("SELECT pg_try_advisory_lock(:key)"),
                {"key": _MIGRATION_LOCK_KEY},
            )
            assert acquired_after_outer_release.scalar_one() is True
            await contender.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": _MIGRATION_LOCK_KEY},
            )
            await contender.commit()
    finally:
        await engine.dispose()
