"""DB Session を FastAPI の依存性注入へ載せるアダプター。"""

from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import open_entry_managed_session


async def get_entry_managed_session(
    request: Request,
) -> AsyncGenerator[AsyncSession]:
    """API用Engineから入口管理のSessionを提供する。"""
    async with open_entry_managed_session(request.app.state.engine) as session:
        yield session


# 成功レスポンスを送信する前にトランザクションを確定する。
EntryManagedSession = Annotated[
    AsyncSession, Depends(get_entry_managed_session, scope="function")
]


async def get_caller_managed_session(
    request: Request,
) -> AsyncGenerator[AsyncSession]:
    """API用factoryから処理側管理のSessionを提供する。"""
    async with request.app.state.session_factory() as session:
        yield session
