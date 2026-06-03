"""application_name が実 Postgres に届くことを検証する。"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.config import settings
from app.db_ssl import create_app_engine
from app.queue.lifecycle import worker_service_name

_ADMIN_DB_URL = settings.migration_database_url or settings.database_url
_TEST_DATABASE_URL = _ADMIN_DB_URL.rsplit("/", 1)[0] + "/vector_test"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_application_name_reaches_postgres_current_setting() -> None:
    service_name = worker_service_name("content")
    engine = create_app_engine(_TEST_DATABASE_URL, application_name=service_name)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT current_setting('application_name')")
            )
            assert result.scalar_one() == service_name
    finally:
        await engine.dispose()
