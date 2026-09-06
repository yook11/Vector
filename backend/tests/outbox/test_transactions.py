from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.outbox.repository import OutboxDeliveryRepository
from tests.outbox.helpers import FUTURE, PAST, insert_event, read_event

pytestmark = pytest.mark.asyncio


async def test_concurrent_claim_skips_rows_locked_by_another_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """先行する確保が未commitでも、別担当は残りの行を確保できる。"""
    async with session_factory() as seed:
        ready_ids = {await insert_event(seed, next_attempt_at=PAST) for _ in range(3)}
        await seed.commit()

    async with session_factory() as first, session_factory() as second:
        first_claim = await OutboxDeliveryRepository(first).claim_ready_batch(
            limit=1, lease_duration=timedelta(minutes=5)
        )
        # ロックをスキップしない実装でも、テストを無期限に待機させない。
        await second.execute(text("SET LOCAL lock_timeout = '1s'"))
        await second.execute(text("SET LOCAL statement_timeout = '5s'"))
        second_claim = await OutboxDeliveryRepository(second).claim_ready_batch(
            limit=10, lease_duration=timedelta(minutes=5)
        )
        first_ids = {event.event_id for event in first_claim}
        second_ids = {event.event_id for event in second_claim}
        assert len(first_claim) == len(first_ids) == 1
        assert len(second_claim) == len(second_ids) == 2
        assert first_ids.isdisjoint(second_ids)
        assert first_ids | second_ids == ready_ids


@pytest.mark.parametrize("commit", [True, False], ids=["commit", "rollback"])
async def test_claim_persistence_is_controlled_by_caller(
    session_factory: async_sessionmaker[AsyncSession], commit: bool
) -> None:
    """確保したleaseと試行回数は、呼び出し元が確定した場合だけ残る。"""
    async with session_factory() as seed:
        event_id = await insert_event(seed, next_attempt_at=PAST, attempt_count=2)
        original = await read_event(seed, event_id)
        await seed.commit()

    async with session_factory() as writer:
        await OutboxDeliveryRepository(writer).claim_ready_batch(
            limit=1, lease_duration=timedelta(minutes=5)
        )
        pending = await read_event(writer, event_id)
        assert pending != original
        async with session_factory() as reader:
            assert await read_event(reader, event_id) == original
        if commit:
            await writer.commit()
        else:
            await writer.rollback()

    async with session_factory() as reader:
        saved = await read_event(reader, event_id)
    assert saved == (pending if commit else original)


@pytest.mark.parametrize("commit", [True, False], ids=["commit", "rollback"])
@pytest.mark.parametrize(
    ("operation", "arguments", "changed_column"),
    [
        ("mark_published", {}, "published_at"),
        (
            "schedule_retry",
            {"retry_delay": timedelta(minutes=5)},
            "next_attempt_at",
        ),
        ("stop_delivery", {"reason": "max_attempts"}, "delivery_stopped_at"),
    ],
)
async def test_delivery_update_persistence_is_controlled_by_caller(
    session_factory: async_sessionmaker[AsyncSession],
    commit: bool,
    operation: str,
    arguments: dict[str, Any],
    changed_column: str,
) -> None:
    """配信結果とlease解除は、呼び出し元が確定した場合だけ残る。"""
    token = uuid4()
    async with session_factory() as seed:
        event_id = await insert_event(
            seed,
            next_attempt_at=PAST,
            lease_token=token,
            leased_until=FUTURE,
            attempt_count=3,
        )
        original = await read_event(seed, event_id)
        await seed.commit()

    async with session_factory() as writer:
        updated = await getattr(OutboxDeliveryRepository(writer), operation)(
            event_id=event_id, lease_token=token, **arguments
        )
        assert updated is True
        pending = await read_event(writer, event_id)
        assert pending[changed_column] is not None
        assert pending[changed_column] != original[changed_column]
        assert pending["lease_token"] is None
        assert pending["leased_until"] is None
        async with session_factory() as reader:
            assert await read_event(reader, event_id) == original
        if commit:
            await writer.commit()
        else:
            await writer.rollback()

    async with session_factory() as reader:
        saved = await read_event(reader, event_id)
    assert saved == (pending if commit else original)
