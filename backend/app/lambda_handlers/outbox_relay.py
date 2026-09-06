"""relay基盤のDB接続確認用入口であり、イベント配信はまだ行わない。"""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from app.db.engine import create_lambda_engine
from app.lambda_handlers.settings import OutboxRelaySettings


async def _check_database(settings: OutboxRelaySettings) -> dict[str, str]:
    engine = create_lambda_engine(settings)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return {"check": "database_connectivity", "status": "ok"}
    finally:
        await engine.dispose()


def handler(event: object, context: object) -> dict[str, str]:
    """設定とDB接続だけを確認し、配信状態は変更しない。"""
    settings = OutboxRelaySettings()  # type: ignore[call-arg]
    return asyncio.run(_check_database(settings))
