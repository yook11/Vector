"""migration runner を一時Alembic scriptと実PostgreSQLで検証する。"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.migration_config import MigrationRunnerRequest
from app.migration_lock import migration_advisory_lock
from scripts import migration_runner as runner

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_TREE_OID = "b" * 40


@dataclass(frozen=True)
class AlembicFixture:
    config: Config
    schema: str


def _request(
    mode: str,
    *,
    start: str | None,
    target: str,
) -> MigrationRunnerRequest:
    return MigrationRunnerRequest(
        protocol_version=1,
        mode=mode,
        expected_start_revision=start,
        target_revision=target,
        migration_tree_oid=_TREE_OID,
    )


def _write_fixture(
    directory: Path,
    schema: str,
    *,
    include_contract: bool,
    failing_contract: bool,
) -> Config:
    versions = directory / "versions"
    versions.mkdir(parents=True)
    (directory / "env.py").write_text(
        """
from alembic import context

config = context.config
connection = config.attributes["connection"]
context.configure(
    connection=connection,
    version_table_schema=config.attributes["version_table_schema"],
)
with context.begin_transaction():
    context.run_migrations()
""".lstrip(),
        encoding="utf-8",
    )
    (versions / "r0_baseline.py").write_text(
        """
revision = "r0"
down_revision = None
MIGRATION_KIND = "expand"

def upgrade():
    pass

def downgrade():
    pass
""".lstrip(),
        encoding="utf-8",
    )
    (versions / "r1_expand.py").write_text(
        f"""
from alembic import op
import sqlalchemy as sa

revision = "r1"
down_revision = "r0"
MIGRATION_KIND = "expand"

def upgrade():
    op.create_table(
        "runner_data",
        sa.Column("id", sa.Integer(), primary_key=True),
        schema={schema!r},
    )

def downgrade():
    op.drop_table("runner_data", schema={schema!r})
""".lstrip(),
        encoding="utf-8",
    )
    if include_contract:
        contract_body = (
            '    op.execute("SELECT 1 / 0")\n'
            if failing_contract
            else """    op.execute(
        "DO $$ BEGIN "
        "IF NOT EXISTS ("
        "SELECT 1 FROM pg_locks "
        "WHERE locktype = 'advisory' AND pid = pg_backend_pid() "
        "AND classid = 22085 "
        "AND objid = 1129598802 "
        "AND objsubid = 1"
        ") THEN RAISE EXCEPTION 'outer migration lock is absent'; END IF; "
        "END $$"
    )
    op.add_column(
        "runner_data",
        sa.Column("contract_marker", sa.Integer(), nullable=True),
        schema=__SCHEMA__,
    )
"""
        )
        (versions / "r2_contract.py").write_text(
            """
from alembic import op
import sqlalchemy as sa

revision = "r2"
down_revision = "r1"
MIGRATION_KIND = "contract"

def upgrade():
{contract_body}

def downgrade():
    pass
""".format(contract_body=contract_body.replace("__SCHEMA__", repr(schema))).lstrip(),
            encoding="utf-8",
        )

    config = Config()
    config.set_main_option("script_location", str(directory))
    config.attributes["version_table_schema"] = schema
    return config


@pytest.fixture
async def alembic_fixture(
    tmp_path: Path,
    test_database_url: str,
) -> AsyncIterator[tuple[AsyncEngine, AlembicFixture]]:
    schema = f"migration_runner_{uuid.uuid4().hex}"
    engine = create_async_engine(test_database_url, poolclass=NullPool)
    async with engine.begin() as connection:
        await connection.execute(CreateSchema(schema))
    try:
        yield (
            engine,
            AlembicFixture(
                config=_write_fixture(
                    tmp_path / "alembic",
                    schema,
                    include_contract=True,
                    failing_contract=False,
                ),
                schema=schema,
            ),
        )
    finally:
        async with engine.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True))
        await engine.dispose()


def _stamp(connection: object, fixture: AlembicFixture, revision: str) -> None:
    fixture.config.attributes["connection"] = connection
    command.stamp(fixture.config, revision)


def _upgrade(connection: object, fixture: AlembicFixture, revision: str) -> None:
    fixture.config.attributes["connection"] = connection
    command.upgrade(fixture.config, revision)


def _upgrade_while_locked(
    connection: object,
    fixture: AlembicFixture,
    revision: str,
) -> None:
    with migration_advisory_lock(connection):
        _upgrade(connection, fixture, revision)


def _current_head(connection: object) -> tuple[str, ...]:
    return tuple(MigrationContext.configure(connection).get_current_heads())


def _columns(connection: object, fixture: AlembicFixture) -> set[str]:
    inspector = inspect(connection)
    if not inspector.has_table("runner_data", schema=fixture.schema):
        return set()
    return {
        column["name"]
        for column in inspector.get_columns("runner_data", schema=fixture.schema)
    }


async def _set_search_path(connection: AsyncConnection, schema: str) -> None:
    await connection.execute(
        text("SELECT set_config('search_path', :search_path, false)"),
        {"search_path": schema},
    )
    await connection.commit()


async def test_verify_is_read_only_and_requires_current_to_match_target(
    tmp_path: Path,
    test_database_url: str,
) -> None:
    success_schema = f"migration_runner_{uuid.uuid4().hex}"
    mismatch_schema = f"migration_runner_{uuid.uuid4().hex}"
    engine = create_async_engine(test_database_url, poolclass=NullPool)
    success_fixture = AlembicFixture(
        config=_write_fixture(
            tmp_path / "verify_success_alembic",
            success_schema,
            include_contract=True,
            failing_contract=False,
        ),
        schema=success_schema,
    )
    mismatch_fixture = AlembicFixture(
        config=_write_fixture(
            tmp_path / "verify_mismatch_alembic",
            mismatch_schema,
            include_contract=True,
            failing_contract=False,
        ),
        schema=mismatch_schema,
    )
    async with engine.begin() as connection:
        await connection.execute(CreateSchema(success_schema))
        await connection.execute(CreateSchema(mismatch_schema))
    try:
        async with engine.connect() as success, engine.connect() as mismatch:
            await _set_search_path(success, success_schema)
            await _set_search_path(mismatch, mismatch_schema)
            await success.run_sync(_upgrade_while_locked, success_fixture, "r2")
            await mismatch.run_sync(_upgrade, mismatch_fixture, "r1")
            await success.execute(text("SET default_transaction_read_only = on"))
            await mismatch.execute(text("SET default_transaction_read_only = on"))
            await success.commit()
            await mismatch.commit()

            verified = await success.run_sync(
                runner._run_on_connection,
                _request("verify", start=None, target="r2"),
                config=success_fixture.config,
            )
            rejected = await mismatch.run_sync(
                runner._run_on_connection,
                _request("verify", start=None, target="r2"),
                config=mismatch_fixture.config,
            )

        assert (
            verified.result,
            verified.exit_code,
            rejected.result,
            rejected.exit_code,
        ) == ("verified", runner.EXIT_SUCCESS, "rejected", runner.EXIT_INVALID)
    finally:
        async with engine.begin() as connection:
            await connection.execute(DropSchema(success_schema, cascade=True))
            await connection.execute(DropSchema(mismatch_schema, cascade=True))
        await engine.dispose()


async def test_expand_applies_the_pending_revision_to_the_temporary_schema(
    tmp_path: Path,
    test_database_url: str,
) -> None:
    schema = f"migration_runner_{uuid.uuid4().hex}"
    engine = create_async_engine(test_database_url, poolclass=NullPool)
    fixture = AlembicFixture(
        config=_write_fixture(
            tmp_path / "expand_alembic",
            schema,
            include_contract=False,
            failing_contract=False,
        ),
        schema=schema,
    )
    async with engine.begin() as connection:
        await connection.execute(CreateSchema(schema))
    try:
        async with engine.connect() as connection:
            await _set_search_path(connection, schema)
            await connection.run_sync(_stamp, fixture, "r0")
            result = await connection.run_sync(
                runner._run_on_connection,
                _request("expand", start="r0", target="r1"),
                config=fixture.config,
            )
            columns = await connection.run_sync(_columns, fixture)

        assert (result.result, result.exit_code, columns) == (
            "applied",
            runner.EXIT_SUCCESS,
            {"id"},
        )
    finally:
        async with engine.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True))
        await engine.dispose()


async def test_contract_applies_only_while_the_runner_session_holds_its_lock(
    alembic_fixture: tuple[AsyncEngine, AlembicFixture],
) -> None:
    engine, fixture = alembic_fixture
    async with engine.connect() as connection:
        await _set_search_path(connection, fixture.schema)
        await connection.run_sync(_upgrade, fixture, "r1")
        result = await connection.run_sync(
            runner._run_on_connection,
            _request("contract", start="r1", target="r2"),
            config=fixture.config,
        )
        columns = await connection.run_sync(_columns, fixture)

    assert (result.result, result.exit_code, columns) == (
        "applied",
        runner.EXIT_SUCCESS,
        {"id", "contract_marker"},
    )


@pytest.mark.parametrize("case", ["mixed", "unresolvable"])
async def test_real_pending_range_rejects_mixed_or_unresolvable_revisions(
    case: str,
    alembic_fixture: tuple[AsyncEngine, AlembicFixture],
) -> None:
    engine, fixture = alembic_fixture
    expected_start = "missing" if case == "unresolvable" else "r0"
    async with engine.connect() as connection:
        await _set_search_path(connection, fixture.schema)
        await connection.run_sync(_stamp, fixture, "r0")
        if case == "unresolvable":
            await connection.execute(
                text("UPDATE alembic_version SET version_num = :revision"),
                {"revision": "missing"},
            )
            await connection.commit()
        result = await connection.run_sync(
            runner._run_on_connection,
            _request("contract", start=expected_start, target="r2"),
            config=fixture.config,
        )
        head = await connection.run_sync(_current_head)
        columns = await connection.run_sync(_columns, fixture)

    assert (result.result, result.exit_code, head, columns) == (
        "rejected",
        runner.EXIT_INVALID,
        (expected_start,),
        set(),
    )


async def test_lock_conflict_prevents_expand_before_any_ddl(
    tmp_path: Path,
    test_database_url: str,
) -> None:
    schema = f"migration_runner_{uuid.uuid4().hex}"
    engine = create_async_engine(test_database_url, poolclass=NullPool)
    fixture = AlembicFixture(
        config=_write_fixture(
            tmp_path / "lock_alembic",
            schema,
            include_contract=False,
            failing_contract=False,
        ),
        schema=schema,
    )
    async with engine.begin() as connection:
        await connection.execute(CreateSchema(schema))
    manager: AbstractContextManager[None] | None = None
    try:
        async with engine.connect() as owner, engine.connect() as contender:
            await _set_search_path(contender, schema)
            await contender.run_sync(_stamp, fixture, "r0")

            def acquire(sync_connection: object) -> None:
                nonlocal manager
                manager = migration_advisory_lock(sync_connection)
                manager.__enter__()

            await owner.run_sync(acquire)
            result = await contender.run_sync(
                runner._run_on_connection,
                _request("expand", start="r0", target="r1"),
                config=fixture.config,
            )
            assert manager is not None
            await owner.run_sync(lambda _connection: manager.__exit__(None, None, None))
            columns = await contender.run_sync(_columns, fixture)

        assert (result.result, result.exit_code, columns) == (
            "rejected",
            runner.EXIT_LOCK_CONFLICT,
            set(),
        )
    finally:
        async with engine.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True))
        await engine.dispose()


async def test_failing_contract_does_not_advance_the_database_revision(
    tmp_path: Path,
    test_database_url: str,
) -> None:
    schema = f"migration_runner_{uuid.uuid4().hex}"
    engine = create_async_engine(test_database_url, poolclass=NullPool)
    fixture = AlembicFixture(
        config=_write_fixture(
            tmp_path / "failing_alembic",
            schema,
            include_contract=True,
            failing_contract=True,
        ),
        schema=schema,
    )
    async with engine.begin() as connection:
        await connection.execute(CreateSchema(schema))
    try:
        async with engine.connect() as connection:
            await _set_search_path(connection, schema)
            await connection.run_sync(_upgrade, fixture, "r1")
            result = await connection.run_sync(
                runner._run_on_connection,
                _request("contract", start="r1", target="r2"),
                config=fixture.config,
            )
            head = await connection.run_sync(_current_head)
            columns = await connection.run_sync(_columns, fixture)

        assert (result.result, result.exit_code, head, columns) == (
            "failed",
            runner.EXIT_MIGRATION_FAILED,
            ("r1",),
            {"id"},
        )
    finally:
        async with engine.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True))
        await engine.dispose()
