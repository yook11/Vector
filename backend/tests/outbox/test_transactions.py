from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.outbox_event import OutboxEvent
from app.outbox.publish_retry_policy import MAX_PUBLISH_ATTEMPTS
from app.outbox.repository import OutboxDeliveryRepository
from tests.outbox.helpers import (
    FUTURE,
    PAST,
    insert_event,
    read_event,
    seed_batch_events,
)

pytestmark = pytest.mark.asyncio
EVENT_TYPE = "article.assessed_in_scope"
LEASE = timedelta(minutes=5)


async def test_concurrent_claim_skips_rows_locked_by_another_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """先行する確保が未commitでも、別担当は残りの行を確保できる。"""
    async with session_factory() as seed:
        ready_ids = {await insert_event(seed, next_attempt_at=PAST) for _ in range(3)}
        await seed.commit()

    async with session_factory() as first, session_factory() as second:
        first_claim = await OutboxDeliveryRepository(first).claim_ready_batch(
            event_type="test.outbox", limit=1, lease_duration=timedelta(minutes=5)
        )
        # ロックをスキップしない実装でも、テストを無期限に待機させない。
        await second.execute(text("SET LOCAL lock_timeout = '1s'"))
        await second.execute(text("SET LOCAL statement_timeout = '5s'"))
        second_claim = await OutboxDeliveryRepository(second).claim_ready_batch(
            event_type="test.outbox", limit=10, lease_duration=timedelta(minutes=5)
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
            event_type="test.outbox", limit=1, lease_duration=timedelta(minutes=5)
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


async def _operate(repository, operation, **kwargs):
    if operation == "claim":
        return await repository.claim_ready_batch(lease_duration=LEASE, **kwargs)
    return await repository.stop_deliveries_at_attempt_limit(**kwargs)


async def test_exhausted_events_left_unstopped_are_not_claimed(db_session):
    """停止件数の上限で残ったイベントを、送信用に再確保しない。"""
    before = await seed_batch_events(
        db_session, event_type=EVENT_TYPE, count=3, attempt_count=MAX_PUBLISH_ATTEMPTS
    )
    repository = OutboxDeliveryRepository(db_session)
    stopped = await repository.stop_deliveries_at_attempt_limit(
        event_type=EVENT_TYPE, limit=1
    )
    remaining = before.keys() - set(stopped)
    assert len(remaining) == 2
    assert (
        await repository.claim_ready_batch(event_type=EVENT_TYPE, lease_duration=LEASE)
        == []
    )
    for event_id in remaining:
        assert await read_event(db_session, event_id) == before[event_id]


@pytest.mark.parametrize("commit", [True, False])
async def test_stop_changes_are_visible_only_after_commit(session_factory, commit):
    """停止結果は返却時には未確定で、commitだけが別sessionへ公開する。"""
    async with session_factory() as seed:
        event_id = await insert_event(
            seed,
            next_attempt_at=FUTURE,
            event_type=EVENT_TYPE,
            attempt_count=5,
            lease_token=uuid4(),
            leased_until=PAST,
        )
        before = await read_event(seed, event_id)
        await seed.commit()
    async with session_factory() as writer:
        assert await OutboxDeliveryRepository(writer).stop_deliveries_at_attempt_limit(
            event_type=EVENT_TYPE
        ) == [event_id]
        pending = await read_event(writer, event_id)
        async with session_factory() as reader:
            assert await read_event(reader, event_id) == before
        if commit:
            await writer.commit()
        else:
            await writer.rollback()
    async with session_factory() as reader:
        assert await read_event(reader, event_id) == (pending if commit else before)


@pytest.mark.parametrize("operation", ["claim", "stop"])
async def test_oldest_locked_row_is_skipped(session_factory, operation):
    """最古の行がロックされていても、次に古い対象を待機せず処理する。"""
    attempt = 0 if operation == "claim" else 5
    async with session_factory() as seed:
        oldest = await insert_event(
            seed,
            next_attempt_at=PAST,
            occurred_at=PAST,
            event_type=EVENT_TYPE,
            attempt_count=attempt,
        )
        newer = await insert_event(
            seed,
            next_attempt_at=PAST,
            occurred_at=PAST + timedelta(seconds=1),
            event_type=EVENT_TYPE,
            attempt_count=attempt,
        )
        await seed.commit()
    async with session_factory() as locker, session_factory() as worker:
        await locker.execute(
            select(OutboxEvent.event_id)
            .where(OutboxEvent.event_id == oldest)
            .with_for_update()
        )
        await worker.execute(text("SET LOCAL lock_timeout = '1s'"))
        await worker.execute(text("SET LOCAL statement_timeout = '5s'"))
        result = await _operate(
            OutboxDeliveryRepository(worker), operation, event_type=EVENT_TYPE, limit=1
        )
        assert (
            [event.event_id for event in result] if operation == "claim" else result
        ) == [newer]
        await worker.commit()
        await locker.rollback()
    async with session_factory() as next_worker:
        result = await _operate(
            OutboxDeliveryRepository(next_worker),
            operation,
            event_type=EVENT_TYPE,
            limit=1,
        )
        assert (
            [event.event_id for event in result] if operation == "claim" else result
        ) == [oldest]


async def test_concurrent_stoppers_update_disjoint_events(session_factory):
    """別の停止処理が更新中の行を避け、各イベントを一度だけ停止する。"""
    async with session_factory() as seed:
        event_ids = {
            await insert_event(
                seed, next_attempt_at=PAST, event_type=EVENT_TYPE, attempt_count=5
            )
            for _ in range(3)
        }
        await seed.commit()
    async with session_factory() as first, session_factory() as second:
        first_ids = await OutboxDeliveryRepository(
            first
        ).stop_deliveries_at_attempt_limit(event_type=EVENT_TYPE, limit=1)
        await second.execute(text("SET LOCAL lock_timeout = '1s'"))
        second_ids = await OutboxDeliveryRepository(
            second
        ).stop_deliveries_at_attempt_limit(event_type=EVENT_TYPE)
        assert len(first_ids) == 1 and len(second_ids) == 2
        assert set(first_ids).isdisjoint(second_ids)
        assert set(first_ids + second_ids) == event_ids
        await first.commit()
        await second.commit()
    async with session_factory() as reader:
        assert (
            await OutboxDeliveryRepository(reader).stop_deliveries_at_attempt_limit(
                event_type=EVENT_TYPE
            )
            == []
        )


async def test_fifth_claim_is_protected_until_expiry_and_never_reclaimed(
    session_factory,
):
    """5回目の確保中・確定後を保護し、異常終了後は再確保せず停止する。"""
    async with session_factory() as seed:
        event_id = await insert_event(
            seed, next_attempt_at=PAST, event_type=EVENT_TYPE, attempt_count=4
        )
        await seed.commit()
    async with session_factory() as claimant, session_factory() as stopper:
        fifth = (
            await OutboxDeliveryRepository(claimant).claim_ready_batch(
                event_type=EVENT_TYPE, lease_duration=LEASE
            )
        )[0]
        assert fifth.attempt_count == 5
        await stopper.execute(text("SET LOCAL lock_timeout = '1s'"))
        assert (
            await OutboxDeliveryRepository(stopper).stop_deliveries_at_attempt_limit(
                event_type=EVENT_TYPE
            )
            == []
        )
        await claimant.commit()
        assert (
            await OutboxDeliveryRepository(stopper).stop_deliveries_at_attempt_limit(
                event_type=EVENT_TYPE
            )
            == []
        )
        await stopper.rollback()
    async with session_factory() as expiry:
        await expiry.execute(
            update(OutboxEvent)
            .where(OutboxEvent.event_id == event_id)
            .values(leased_until=PAST)
        )
        await expiry.commit()
    async with session_factory() as stopper, session_factory() as claimant:
        repository = OutboxDeliveryRepository(stopper)
        assert await repository.stop_deliveries_at_attempt_limit(
            event_type=EVENT_TYPE
        ) == [event_id]
        await claimant.execute(text("SET LOCAL lock_timeout = '1s'"))
        assert (
            await OutboxDeliveryRepository(claimant).claim_ready_batch(
                event_type=EVENT_TYPE, lease_duration=LEASE
            )
            == []
        )
        await stopper.commit()
        assert not await OutboxDeliveryRepository(claimant).mark_published(
            event_id=event_id, lease_token=fifth.lease_token
        )


@pytest.mark.parametrize("operation", ["claim", "stop"])
async def test_database_failure_propagates_and_can_be_rolled_back(
    session_factory, operation
):
    """失敗したDB操作を送信失敗へ変換せず、未確定の変更はrollbackできる。"""
    from sqlalchemy.exc import DBAPIError

    attempt = 0 if operation == "claim" else 5
    async with session_factory() as seed:
        event_id = await insert_event(
            seed, next_attempt_at=PAST, event_type=EVENT_TYPE, attempt_count=attempt
        )
        before = await read_event(seed, event_id)
        await seed.commit()
    async with session_factory() as writer:
        await writer.execute(text("SET TRANSACTION READ ONLY"))
        with pytest.raises(DBAPIError):
            await _operate(
                OutboxDeliveryRepository(writer), operation, event_type=EVENT_TYPE
            )
        await writer.rollback()
    async with session_factory() as reader:
        assert await read_event(reader, event_id) == before
