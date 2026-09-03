"""DB Session を FastAPI の依存性注入へ載せるアダプター。"""

from collections.abc import AsyncGenerator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import open_entry_managed_session


async def get_entry_managed_session(
    request: Request,
) -> AsyncGenerator[AsyncSession]:
    """API用Engineから入口管理のSessionを提供する。"""
    async with open_entry_managed_session(request.app.state.engine) as session:
        yield session


async def get_caller_managed_session(
    request: Request,
) -> AsyncGenerator[AsyncSession]:
    """API用factoryから処理側管理のSessionを提供する。"""
    async with request.app.state.session_factory() as session:
        yield session
