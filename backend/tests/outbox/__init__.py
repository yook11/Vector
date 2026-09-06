from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AsyncExitStack
from uuid import uuid4

import pytest
from sqlalchemy import CheckConstraint, Column, MetaData, Table, Text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.schema import AddConstraint, DropConstraint

type RejectOutboxInsert = Callable[[str], Awaitable[str]]


@pytest.fixture
async def reject_outbox_insert(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[RejectOutboxInsert]:
    """指定イベントのINSERTをDBで拒否し、追加した制約をテスト終了時に解除する。"""

    async def remove_constraint(constraint: CheckConstraint) -> None:
        async with session_factory() as admin:
            await admin.execute(DropConstraint(constraint))
            await admin.commit()

    async with AsyncExitStack() as cleanup:

        async def reject(event_type: str) -> str:
            name = f"test_reject_outbox_insert_{uuid4().hex}"
            # 実モデルのmetadataを変えず、テストDBだけに拒否制約を追加する。
            table = Table("outbox_events", MetaData(), Column("event_type", Text))
            constraint = CheckConstraint(table.c.event_type != event_type, name=name)
            async with session_factory() as admin:
                await admin.execute(AddConstraint(constraint))
                await admin.commit()
            cleanup.push_async_callback(remove_constraint, constraint)
            return name

        yield reject
