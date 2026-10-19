"""実PostgreSQLに、再試行で解消しうるDB障害と解消しないDB障害を起こす。"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def cancel_statement(session: AsyncSession) -> None:
    """statement_timeout で文を取り消させ、再試行で解消しうる 57014 を起こす。"""
    await session.execute(text("SET LOCAL statement_timeout = '1ms'"))
    await session.execute(text("SELECT pg_sleep(1)"))


async def divide_by_zero(session: AsyncSession) -> None:
    """値の誤りとして、再試行で解消しない 22012 を起こす。"""
    await session.execute(text("SELECT 1 / 0"))
