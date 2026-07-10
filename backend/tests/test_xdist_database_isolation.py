"""pytest-xdist worker ごとの Postgres 分離を検証する。"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import _CreateDatabase, _test_database_name_for_worker


@pytest.mark.parametrize(
    ("worker_id", "expected"),
    [
        (None, "vector_test"),
        ("master", "vector_test"),
        ("gw0", "vector_test_gw0"),
        ("gw12", "vector_test_gw12"),
    ],
)
def test_database_name_is_derived_from_xdist_worker(
    worker_id: str | None, expected: str
) -> None:
    assert _test_database_name_for_worker(worker_id) == expected


@pytest.mark.parametrize("worker_id", ["", "gw-1", "worker0", "gw0;drop"])
def test_database_name_rejects_untrusted_worker_id(worker_id: str) -> None:
    with pytest.raises(ValueError, match="invalid pytest-xdist worker id"):
        _test_database_name_for_worker(worker_id)


def test_create_database_ddl_quotes_identifier() -> None:
    statement = _CreateDatabase('vector_test_bad"name')

    assert str(statement.compile(dialect=postgresql.dialect())) == (
        'CREATE DATABASE "vector_test_bad""name"'
    )


@pytest.mark.asyncio
async def test_db_session_uses_current_worker_database(
    db_session: AsyncSession,
    worker_id: str,
) -> None:
    result = await db_session.execute(text("SELECT current_database()"))

    assert result.scalar_one() == _test_database_name_for_worker(worker_id)
