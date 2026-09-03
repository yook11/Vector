"""Engine に紐づく Session とトランザクション管理の入口。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


@asynccontextmanager
async def open_entry_managed_session(
    engine: AsyncEngine,
) -> AsyncIterator[AsyncSession]:
    """入口がトランザクションの開始・確定・取消・クローズまで担う。

    Repository は commit / refresh を呼ばず、ID払い出しが必要な場合だけflushする。
    正常終了時は変更をまとめて永続化し、例外時はトランザクション全体を取り消す。
    """
    async with AsyncSession(engine, expire_on_commit=True) as session:
        async with session.begin():
            yield session


def caller_managed_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Session の開閉だけを担う factory。begin / commit は処理側。"""
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
