"""application_name が実 Postgres に届くことを検証する。"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.config import settings
from app.db.engine import create_cli_engine, worker_service_name


@pytest.mark.integration
@pytest.mark.asyncio
async def test_application_name_reaches_postgres_current_setting(
    test_database_url: str,
) -> None:
    service_name = worker_service_name("collection")
    engine = create_cli_engine(
        settings,
        service_name,
        database_url=test_database_url,
        use_configured_auth=False,
    )
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT current_setting('application_name')")
            )
            assert result.scalar_one() == service_name
    finally:
        await engine.dispose()
