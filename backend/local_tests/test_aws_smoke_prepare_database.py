"""AWS接続を代替せず、prepareの実DB判定と再実行の保持を確認する。"""

import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from asyncpg import DivisionByZeroError

from app.migration_lock import _MIGRATION_LOCK_KEY
from local_tests.database import migration_heads

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "infra/aws-test/scripts"))
from smoke_runner import prepare_database as prepare  # noqa: E402


async def test_prepared_database_recheck_preserves_data(system_database, monkeypatch):
    forbidden = AsyncMock(
        side_effect=AssertionError("prepared DB must not be initialized")
    )
    monkeypatch.setattr(prepare, "provision_roles", forbidden)
    monkeypatch.setattr(prepare, "prepare_auth", forbidden)
    async with system_database.connect("vector") as master:
        before = await master.fetch("SELECT id, name FROM categories ORDER BY id")
        result = await prepare.prepare_locked(
            master, system_database.connect, forbidden, migration_heads()
        )
        assert result == {
            "status": "prepared",
            "heads": sorted(migration_heads()),
            "verified": True,
            "action": "verified",
        }
        assert (
            await master.fetch("SELECT id, name FROM categories ORDER BY id") == before
        )
    forbidden.assert_not_called()


@pytest.mark.parametrize(
    "damage", ["revision", "seed", "auth", "permission", "partial"]
)
async def test_incomplete_database_is_rejected_without_repair(
    system_database, monkeypatch, damage
):
    forbidden = AsyncMock(
        side_effect=AssertionError("incomplete DB must not be repaired")
    )
    monkeypatch.setattr(prepare, "provision_roles", forbidden)
    monkeypatch.setattr(prepare, "prepare_auth", forbidden)
    async with system_database.connect("vector") as master:
        if damage == "revision":
            await master.execute(
                "UPDATE alembic_version SET version_num = 'wrong-revision'"
            )
        elif damage == "seed":
            await master.execute("DELETE FROM categories")
        elif damage == "auth":
            await master.execute("DROP TABLE auth.verification")
        elif damage == "permission":
            await master.execute("REVOKE SELECT ON analyzed_articles FROM vector_app")
        else:
            await master.execute("DROP TABLE alembic_version")
        with pytest.raises(
            prepare.PreparationError, match="destroy_then_use_new_run_id"
        ):
            await prepare.prepare_locked(
                master, system_database.connect, forbidden, migration_heads()
            )
    forbidden.assert_not_called()


async def test_fresh_database_and_auth_only_state(system_database):
    async with replace(system_database, name="postgres").connect(
        "vector"
    ) as connection:
        assert await prepare.database_state(connection) == "fresh"
        transaction = connection.transaction()
        await transaction.start()
        try:
            await connection.execute("CREATE SCHEMA auth")
            with pytest.raises(prepare.PreparationError, match="database_incomplete"):
                await prepare.database_state(connection)
        finally:
            await transaction.rollback()


async def test_preparation_lock_rejects_overlap_and_releases(system_database):
    forbidden = AsyncMock(
        side_effect=AssertionError("concurrent preparation must stop")
    )
    async with (
        system_database.connect("vector") as first,
        system_database.connect("vector") as second,
    ):
        async with prepare.preparation_lock(first):
            assert await second.fetchval(
                "SELECT pg_try_advisory_lock($1)", _MIGRATION_LOCK_KEY
            )
            await second.fetchval("SELECT pg_advisory_unlock($1)", _MIGRATION_LOCK_KEY)
            with pytest.raises(
                prepare.PreparationError, match="database_prepare_in_progress"
            ):
                await prepare.prepare_locked(
                    second, forbidden, forbidden, migration_heads()
                )
        async with prepare.preparation_lock(second):
            pass
        with pytest.raises(DivisionByZeroError):
            async with prepare.preparation_lock(first):
                await first.execute("BEGIN")
                await first.execute("SELECT 1 / 0")
        async with prepare.preparation_lock(second):
            pass
    forbidden.assert_not_called()
