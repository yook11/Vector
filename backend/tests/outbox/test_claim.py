from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.outbox.repository import OutboxDeliveryRepository
from tests.outbox.helpers import FUTURE, PAST, insert_event, read_event

pytestmark = pytest.mark.asyncio


async def test_claim_ready_batch_skips_published_event(
    db_session: AsyncSession,
) -> None:
    """送信済みの記録は確保しない。"""
    ready_id = await insert_event(
        db_session,
        next_attempt_at=PAST,
    )
    await insert_event(
        db_session,
        next_attempt_at=PAST,
        published_at=PAST,
    )
    claimed = await OutboxDeliveryRepository(db_session).claim_ready_batch(
        limit=10,
        lease_duration=timedelta(minutes=5),
    )
    assert {item.event_id for item in claimed} == {ready_id}


async def test_claim_ready_batch_skips_stopped_event(
    db_session: AsyncSession,
) -> None:
    """配信停止済みの記録は確保しない。"""
    ready_id = await insert_event(
        db_session,
        next_attempt_at=PAST,
    )
    await insert_event(
        db_session,
        next_attempt_at=PAST,
        delivery_stopped_at=PAST,
        delivery_stop_reason="max_attempts",
    )
    claimed = await OutboxDeliveryRepository(db_session).claim_ready_batch(
        limit=10,
        lease_duration=timedelta(minutes=5),
    )
    assert {item.event_id for item in claimed} == {ready_id}


async def test_claim_ready_batch_skips_event_waiting_for_retry(
    db_session: AsyncSession,
) -> None:
    """再試行時刻が来ていない記録は確保しない。"""
    ready_id = await insert_event(
        db_session,
        next_attempt_at=PAST,
    )
    await insert_event(
        db_session,
        next_attempt_at=FUTURE,
    )
    claimed = await OutboxDeliveryRepository(db_session).claim_ready_batch(
        limit=10,
        lease_duration=timedelta(minutes=5),
    )
    assert {item.event_id for item in claimed} == {ready_id}


async def test_claim_ready_batch_skips_event_with_active_lease(
    db_session: AsyncSession,
) -> None:
    """有効なleaseがある記録は確保しない。"""
    ready_id = await insert_event(
        db_session,
        next_attempt_at=PAST,
    )
    await insert_event(
        db_session,
        next_attempt_at=PAST,
        lease_token=uuid4(),
        leased_until=FUTURE,
    )
    claimed = await OutboxDeliveryRepository(db_session).claim_ready_batch(
        limit=10,
        lease_duration=timedelta(minutes=5),
    )
    assert {item.event_id for item in claimed} == {ready_id}


async def test_claim_respects_limit_without_selecting_specific_ids(
    db_session: AsyncSession,
) -> None:
    """上限を超える対象がある場合も、件数と対象条件だけを保証する。"""
    ready_ids = {await insert_event(db_session, next_attempt_at=PAST) for _ in range(3)}
    await insert_event(db_session, next_attempt_at=FUTURE)

    claimed = await OutboxDeliveryRepository(db_session).claim_ready_batch(
        limit=2, lease_duration=timedelta(minutes=5)
    )

    claimed_ids = {item.event_id for item in claimed}
    assert len(claimed) == len(claimed_ids) == 2
    assert claimed_ids <= ready_ids


async def test_claim_returns_empty_when_no_event_is_ready(
    db_session: AsyncSession,
) -> None:
    """対象外の行しかない場合は空リストを返す。"""
    await insert_event(db_session, next_attempt_at=FUTURE)
    claimed = await OutboxDeliveryRepository(db_session).claim_ready_batch(
        limit=10, lease_duration=timedelta(minutes=5)
    )
    assert claimed == []


async def test_claim_updates_only_lease_and_attempt_count(
    db_session: AsyncSession,
) -> None:
    """確保はleaseを設定し、試行回数を1増やし、他の列を維持する。"""
    event_id = await insert_event(
        db_session,
        next_attempt_at=PAST,
        attempt_count=2,
        event_type="article.assessed_in_scope",
        payload={"curation_id": 123, "analyzed_article_id": 456},
    )
    before = await read_event(db_session, event_id)

    await OutboxDeliveryRepository(db_session).claim_ready_batch(
        limit=10, lease_duration=timedelta(minutes=5)
    )

    saved = await read_event(db_session, event_id)
    assert isinstance(saved["lease_token"], UUID)
    assert saved["leased_until"] is not None
    assert saved == {
        **before,
        "lease_token": saved["lease_token"],
        "leased_until": saved["leased_until"],
        "attempt_count": 3,
    }


async def test_claim_sets_lease_expiry_from_database_time(
    db_session: AsyncSession,
) -> None:
    """lease期限は確保時のDB時刻から指定期間後になる。"""
    event_id = await insert_event(db_session, next_attempt_at=PAST)
    started_at = await db_session.scalar(select(func.statement_timestamp()))

    await OutboxDeliveryRepository(db_session).claim_ready_batch(
        limit=10, lease_duration=timedelta(minutes=5)
    )

    finished_at = await db_session.scalar(select(func.statement_timestamp()))
    saved = await read_event(db_session, event_id)
    assert started_at is not None and finished_at is not None
    assert (
        started_at + timedelta(minutes=5)
        <= saved["leased_until"]
        <= finished_at + timedelta(minutes=5)
    )


async def test_claim_returns_saved_values(
    db_session: AsyncSession,
) -> None:
    """返却値は確保後にDBへ保存された各項目と一致する。"""
    payload = {"curation_id": 123, "analyzed_article_id": 456}
    event_id = await insert_event(
        db_session,
        next_attempt_at=PAST,
        attempt_count=2,
        event_type="article.assessed_in_scope",
        payload=payload,
    )
    claimed = await OutboxDeliveryRepository(db_session).claim_ready_batch(
        limit=10, lease_duration=timedelta(minutes=5)
    )
    saved = await read_event(db_session, event_id)

    assert len(claimed) == 1
    event = claimed[0]
    assert event.event_id == saved["event_id"]
    assert event.event_type == saved["event_type"]
    assert event.schema_version == saved["schema_version"]
    assert event.payload == saved["payload"]
    assert event.occurred_at == saved["occurred_at"]
    assert event.attempt_count == saved["attempt_count"]
    assert event.lease_token == saved["lease_token"]
    assert event.leased_until == saved["leased_until"]


async def test_reclaim_replaces_expired_token_and_increments_attempt_count(
    db_session: AsyncSession,
) -> None:
    """期限切れの担当を置き換え、既存の試行回数を引き継ぐ。"""
    old_token = uuid4()
    event_id = await insert_event(
        db_session,
        next_attempt_at=PAST,
        lease_token=old_token,
        leased_until=PAST,
        attempt_count=4,
    )
    claimed = await OutboxDeliveryRepository(db_session).claim_ready_batch(
        limit=10, lease_duration=timedelta(minutes=5)
    )

    assert len(claimed) == 1
    event = claimed[0]
    assert event.event_id == event_id
    assert isinstance(event.lease_token, UUID)
    assert event.lease_token != old_token
    assert event.attempt_count == 5
    saved = await read_event(db_session, event_id)
    assert saved["lease_token"] == event.lease_token
    assert saved["leased_until"] == event.leased_until
    assert saved["attempt_count"] == 5


async def test_claim_generates_a_different_token_for_each_event(
    db_session: AsyncSession,
) -> None:
    """同じ一括確保でも、各イベントが別のtokenを持つ。"""
    ready_ids = {await insert_event(db_session, next_attempt_at=PAST) for _ in range(2)}
    claimed = await OutboxDeliveryRepository(db_session).claim_ready_batch(
        limit=10, lease_duration=timedelta(minutes=5)
    )
    assert {event.event_id for event in claimed} == ready_ids
    assert len({event.lease_token for event in claimed}) == len(ready_ids)


@pytest.mark.parametrize("limit", [0, -1])
async def test_nonpositive_limit_returns_empty_without_database_access(
    limit: int,
) -> None:
    """DB接続のないsessionでも、非正の上限は空結果を返す。"""
    async with AsyncSession() as session:
        claimed = await OutboxDeliveryRepository(session).claim_ready_batch(
            limit=limit, lease_duration=timedelta(minutes=5)
        )
    assert claimed == []


@pytest.mark.parametrize("limit", [0, 10])
@pytest.mark.parametrize("duration", [timedelta(0), timedelta(seconds=-1)])
async def test_claim_rejects_nonpositive_duration_even_with_zero_limit(
    db_session: AsyncSession, limit: int, duration: timedelta
) -> None:
    """上限がゼロでも、無効なlease期間は受け入れない。"""
    event_id = await insert_event(db_session, next_attempt_at=PAST)
    before = await read_event(db_session, event_id)
    with pytest.raises(ValueError):
        await OutboxDeliveryRepository(db_session).claim_ready_batch(
            limit=limit, lease_duration=duration
        )
    assert await read_event(db_session, event_id) == before
