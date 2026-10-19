"""実PostgreSQLが返したSQLSTATEが、asyncpgとSQLAlchemyを通っても同じ種類と理由へ変換されることを検証する。"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.errors import (
    DatabaseCachedStatementInvalidError,
    DatabaseConnectionError,
    DatabaseConnectionErrorReason,
    DatabaseConstraintError,
    DatabaseConstraintErrorReason,
    DatabaseDataError,
    DatabaseError,
    DatabaseSyntaxOrAccessError,
    DatabaseTimeoutError,
    DatabaseTimeoutErrorReason,
    DatabaseTransactionConflictError,
    DatabaseTransactionConflictErrorReason,
    DatabaseUnexpectedError,
)
from app.db.translate import translate_database_error

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _translated(session: AsyncSession, *statements: str) -> DatabaseError:
    with pytest.raises(SQLAlchemyError) as captured:
        for statement in statements:
            await session.execute(text(statement))
    return translate_database_error(captured.value)


@pytest.mark.parametrize(
    ("statement", "expected_type", "expected_reason"),
    [
        pytest.param(
            "DO $$ BEGIN RAISE EXCEPTION 'injected' USING ERRCODE = '55P03'; END $$",
            DatabaseTimeoutError,
            DatabaseTimeoutErrorReason.LOCK_TIMEOUT,
            id="lock_not_available",
        ),
        pytest.param(
            "DO $$ BEGIN RAISE EXCEPTION 'injected' USING ERRCODE = '40P01'; END $$",
            DatabaseTransactionConflictError,
            DatabaseTransactionConflictErrorReason.DEADLOCK,
            id="deadlock_detected",
        ),
        pytest.param(
            "DO $$ BEGIN RAISE EXCEPTION 'injected' USING ERRCODE = '40001'; END $$",
            DatabaseTransactionConflictError,
            DatabaseTransactionConflictErrorReason.SERIALIZATION_FAILURE,
            id="serialization_failure",
        ),
        pytest.param(
            "DO $$ BEGIN RAISE EXCEPTION 'injected' USING ERRCODE = '23000'; END $$",
            DatabaseConstraintError,
            DatabaseConstraintErrorReason.INTEGRITY_CONSTRAINT_VIOLATION,
            id="integrity_constraint_violation",
        ),
        pytest.param(
            "DO $$ BEGIN RAISE EXCEPTION 'injected' USING ERRCODE = '23505'; END $$",
            DatabaseConstraintError,
            DatabaseConstraintErrorReason.UNIQUE_VIOLATION,
            id="unique_violation",
        ),
        pytest.param(
            "DO $$ BEGIN RAISE EXCEPTION 'injected' USING ERRCODE = '08006'; END $$",
            DatabaseConnectionError,
            DatabaseConnectionErrorReason.CONNECTION_FAILED,
            id="connection_failure",
        ),
    ],
)
async def test_raised_sqlstate_is_translated(
    db_session: AsyncSession,
    statement: str,
    expected_type: type[DatabaseError],
    expected_reason: object,
) -> None:
    """サーバーが返したSQLSTATEを、asyncpgが汎用の例外にしても種類と理由へ変換する。"""
    translated = await _translated(db_session, statement)

    assert type(translated) is expected_type
    assert getattr(translated, "reason", None) == expected_reason


async def test_statement_timeout_is_translated_to_query_canceled(
    db_session: AsyncSession,
) -> None:
    """statement_timeout で取り消された文を、待ちが尽きた失敗の取り消しへ変換する。"""
    translated = await _translated(
        db_session, "SET LOCAL statement_timeout = '1ms'", "SELECT pg_sleep(1)"
    )

    assert type(translated) is DatabaseTimeoutError
    assert getattr(translated, "reason", None) == (
        DatabaseTimeoutErrorReason.QUERY_CANCELED
    )


@pytest.mark.parametrize(
    ("statement", "expected_type"),
    [
        pytest.param(
            "SELECT no_such_column FROM categories",
            DatabaseSyntaxOrAccessError,
            id="undefined_column",
        ),
        pytest.param("SELECT 1 / 0", DatabaseDataError, id="division_by_zero"),
        pytest.param(
            "DO $$ BEGIN RAISE EXCEPTION 'injected' USING ERRCODE = 'XX000'; END $$",
            DatabaseUnexpectedError,
            id="internal_error",
        ),
    ],
)
async def test_statement_data_and_unclassified_errors_are_translated(
    db_session: AsyncSession,
    statement: str,
    expected_type: type[DatabaseError],
) -> None:
    """存在しない列はSQLの誤り、0除算は値の誤り、内部エラーは分類できない失敗へ変換する。"""
    translated = await _translated(db_session, statement)

    assert type(translated) is expected_type


async def test_stale_cached_statement_is_translated_and_next_run_succeeds(
    test_database_url: str,
) -> None:
    """別の接続で列の型を変えた直後の同じSQLはキャッシュ失効になり、次は成功する。"""
    # 同じ engine の DDL では先にキャッシュが捨てられるため、別の engine から流す。
    ddl_engine = create_async_engine(test_database_url, poolclass=NullPool)
    query_engine = create_async_engine(test_database_url, poolclass=NullPool)
    try:
        async with ddl_engine.begin() as ddl:
            await ddl.execute(
                text("CREATE TABLE cached_statement_probe (value integer)")
            )
            await ddl.execute(text("INSERT INTO cached_statement_probe VALUES (1)"))
        async with query_engine.connect() as connection:
            await connection.execute(text("SELECT value FROM cached_statement_probe"))
            await connection.rollback()
            async with ddl_engine.begin() as ddl:
                await ddl.execute(
                    text(
                        "ALTER TABLE cached_statement_probe "
                        "ALTER COLUMN value TYPE bigint"
                    )
                )
            with pytest.raises(SQLAlchemyError) as captured:
                await connection.execute(
                    text("SELECT value FROM cached_statement_probe")
                )
            await connection.rollback()
            retried = await connection.scalar(
                text("SELECT value FROM cached_statement_probe")
            )
    finally:
        async with ddl_engine.begin() as ddl:
            await ddl.execute(text("DROP TABLE IF EXISTS cached_statement_probe"))
        await ddl_engine.dispose()
        await query_engine.dispose()

    assert (
        type(translate_database_error(captured.value))
        is DatabaseCachedStatementInvalidError
    )
    assert retried == 1
