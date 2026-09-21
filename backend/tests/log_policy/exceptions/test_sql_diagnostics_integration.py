"""実PostgreSQLの診断属性がセッション境界を越えてログへ届くことを検証する。"""

import json

import pytest
import structlog
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.errors import DatabaseConstraintError
from app.db.session import open_entry_managed_session
from app.log_policy import BASE_LOG_RULES, PolicyLogger
from app.log_policy.processor import LogPolicyProcessor
from app.models.category import Category

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_ROW_VALUE = "synthetic-private-category-name"


def _log_exception(exc: BaseException) -> dict:
    return LogPolicyProcessor()(
        PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
        "error",
        {"event": "database_operation_failed", "exc_info": exc},
    )


class TestDirectSqlDiagnostics:
    async def test_unique_violation_retains_constraint(
        self, db_session: AsyncSession
    ) -> None:
        """実DBの一意制約違反から対象を残し、重複した行の値を出さない。"""
        await db_session.execute(insert(Category).values(slug="a", name=_ROW_VALUE))
        with pytest.raises(IntegrityError) as captured:
            await db_session.execute(insert(Category).values(slug="b", name=_ROW_VALUE))
        output = _log_exception(captured.value)
        assert output["error_details"] == {
            "kind": "postgresql",
            "sqlstate": "23505",
            "schema_name": "public",
            "table_name": "categories",
            "constraint_name": "categories_name_key",
        }
        assert _ROW_VALUE not in json.dumps(output)
        assert "[SQL:" not in json.dumps(output)
        assert "causes" not in output

    async def test_not_null_violation_retains_column(
        self, db_session: AsyncSession
    ) -> None:
        """実DBのNOT NULL違反から対象カラムを残し、失敗行を出さない。"""
        with pytest.raises(IntegrityError) as captured:
            await db_session.execute(
                insert(Category).values(slug="synthetic_private_slug", name=None)
            )
        output = _log_exception(captured.value)
        assert output["error_details"] == {
            "kind": "postgresql",
            "sqlstate": "23502",
            "schema_name": "public",
            "table_name": "categories",
            "column_name": "name",
        }
        assert "synthetic_private_slug" not in json.dumps(output)
        assert "Failing row" not in json.dumps(output)


class TestSessionWrappedDiagnostics:
    async def test_unique_violation_survives_session_translation(
        self, db_session: AsyncSession
    ) -> None:
        """製品のセッション境界で包まれた一意制約違反をSQL原因として記録する。"""
        with pytest.raises(DatabaseConstraintError) as captured:
            async with open_entry_managed_session(db_session.bind) as session:
                await session.execute(
                    insert(Category).values(slug="a", name=_ROW_VALUE)
                )
                await session.execute(
                    insert(Category).values(slug="b", name=_ROW_VALUE)
                )
        output = _log_exception(captured.value)
        assert output["error_class"] == "app.db.errors.DatabaseConstraintError"
        assert "error_details" not in output
        assert output["causes"][0]["error_details"] == {
            "kind": "postgresql",
            "sqlstate": "23505",
            "schema_name": "public",
            "table_name": "categories",
            "constraint_name": "categories_name_key",
        }
        assert _ROW_VALUE not in json.dumps(output)
        assert "[SQL:" not in json.dumps(output)

    async def test_not_null_violation_survives_session_translation(
        self, db_session: AsyncSession
    ) -> None:
        """製品のセッション境界で包まれたNOT NULL違反でも対象カラムを残す。"""
        with pytest.raises(DatabaseConstraintError) as captured:
            async with open_entry_managed_session(db_session.bind) as session:
                await session.execute(
                    insert(Category).values(slug="synthetic_private_slug", name=None)
                )
        output = _log_exception(captured.value)
        assert output["causes"][0]["error_details"] == {
            "kind": "postgresql",
            "sqlstate": "23502",
            "schema_name": "public",
            "table_name": "categories",
            "column_name": "name",
        }
        assert "synthetic_private_slug" not in json.dumps(output)
        assert "Failing row" not in json.dumps(output)
