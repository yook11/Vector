from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.outbox.delivery.repository import OutboxDeliveryRepository
from app.outbox.delivery.retry_policy import MAX_PUBLISH_ATTEMPTS
from app.outbox.delivery.values import DeliveryBatchSelection, LeaseDuration
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
        selection=DeliveryBatchSelection(event_type="test.outbox", limit=10),
        lease_duration=LeaseDuration(timedelta(minutes=5)),
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
        selection=DeliveryBatchSelection(event_type="test.outbox", limit=10),
        lease_duration=LeaseDuration(timedelta(minutes=5)),
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
        selection=DeliveryBatchSelection(event_type="test.outbox", limit=10),
        lease_duration=LeaseDuration(timedelta(minutes=5)),
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
        selection=DeliveryBatchSelection(event_type="test.outbox", limit=10),
        lease_duration=LeaseDuration(timedelta(minutes=5)),
    )
    assert {item.event_id for item in claimed} == {ready_id}


async def test_claim_respects_limit_without_selecting_specific_ids(
    db_session: AsyncSession,
) -> None:
    """上限を超える対象がある場合も、件数と対象条件だけを保証する。"""
    ready_ids = {await insert_event(db_session, next_attempt_at=PAST) for _ in range(3)}
    await insert_event(db_session, next_attempt_at=FUTURE)

    claimed = await OutboxDeliveryRepository(db_session).claim_ready_batch(
        selection=DeliveryBatchSelection(event_type="test.outbox", limit=2),
        lease_duration=LeaseDuration(timedelta(minutes=5)),
    )

    claimed_ids = {item.event_id for item in claimed}
    assert len(claimed) == len(claimed_ids) == 2
    assert claimed_ids <= ready_ids


@pytest.mark.parametrize("attempt", [0, 4, 5, 6])
async def test_claim_attempt_boundaries(db_session, attempt):
    """確保前が4回なら5回目を返し、5回以上なら行を変更しない。"""
    event_id = await insert_event(
        db_session,
        next_attempt_at=PAST,
        event_type="test.outbox",
        attempt_count=attempt,
    )
    before = await read_event(db_session, event_id)
    result = await OutboxDeliveryRepository(db_session).claim_ready_batch(
        selection=DeliveryBatchSelection(event_type="test.outbox", limit=10),
        lease_duration=LeaseDuration(timedelta(minutes=5)),
    )
    if attempt < MAX_PUBLISH_ATTEMPTS:
        assert len(result) == 1
        assert result[0].attempt_count == attempt + 1
        assert result[0].event_id == event_id
    else:
        assert result == []
        assert await read_event(db_session, event_id) == before


async def test_claim_returns_empty_when_no_event_is_ready(
    db_session: AsyncSession,
) -> None:
    """対象外の行しかない場合は空リストを返す。"""
    await insert_event(db_session, next_attempt_at=FUTURE)
    claimed = await OutboxDeliveryRepository(db_session).claim_ready_batch(
        selection=DeliveryBatchSelection(event_type="test.outbox", limit=10),
        lease_duration=LeaseDuration(timedelta(minutes=5)),
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
        selection=DeliveryBatchSelection(
            event_type="article.assessed_in_scope", limit=10
        ),
        lease_duration=LeaseDuration(timedelta(minutes=5)),
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
        selection=DeliveryBatchSelection(event_type="test.outbox", limit=10),
        lease_duration=LeaseDuration(timedelta(minutes=5)),
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
        selection=DeliveryBatchSelection(
            event_type="article.assessed_in_scope", limit=10
        ),
        lease_duration=LeaseDuration(timedelta(minutes=5)),
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
        selection=DeliveryBatchSelection(event_type="test.outbox", limit=10),
        lease_duration=LeaseDuration(timedelta(minutes=5)),
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
        selection=DeliveryBatchSelection(event_type="test.outbox", limit=10),
        lease_duration=LeaseDuration(timedelta(minutes=5)),
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
            selection=DeliveryBatchSelection(event_type="test.outbox", limit=limit),
            lease_duration=LeaseDuration(timedelta(minutes=5)),
        )
    assert claimed == []


async def test_claim_batches_select_and_return_events_by_occurrence_time(db_session):
    """再試行予定の順番によらず、発生日時の古いイベントから選んで返す。"""
    attempt = 0
    events = {}
    for age in (2, 0, 1):
        events[age] = await insert_event(
            db_session,
            event_type=EVENT_TYPE,
            attempt_count=attempt,
            occurred_at=PAST + timedelta(seconds=age),
            next_attempt_at=PAST + timedelta(seconds=2 - age),
        )
    result = await OutboxDeliveryRepository(db_session).claim_ready_batch(
        selection=DeliveryBatchSelection(event_type=EVENT_TYPE, limit=2),
        lease_duration=LeaseDuration(LEASE),
    )
    ids = [event.event_id for event in result]
    assert ids == [events[0], events[1]]


async def test_claim_equal_occurrence_times_allow_any_event_order(db_session):
    """同時刻に発生したイベントは、IDの順序を要求せず上限件数を選べる。"""
    attempt = 0
    events = {
        await insert_event(
            db_session,
            event_type=EVENT_TYPE,
            attempt_count=attempt,
            occurred_at=PAST,
            next_attempt_at=PAST,
        )
        for _ in range(3)
    }
    result = await OutboxDeliveryRepository(db_session).claim_ready_batch(
        selection=DeliveryBatchSelection(event_type=EVENT_TYPE, limit=2),
        lease_duration=LeaseDuration(LEASE),
    )
    ids = [event.event_id for event in result]
    assert len(ids) == len(set(ids)) == 2
    assert set(ids) <= events


async def test_claim_event_type_is_exact_and_old_events_are_included(db_session):
    """発生日時で除外せず、指定タイプに完全一致する行だけを扱う。"""
    attempt = 0
    target = await insert_event(
        db_session,
        next_attempt_at=PAST,
        occurred_at=PAST,
        event_type=EVENT_TYPE,
        attempt_count=attempt,
    )
    other = await insert_event(
        db_session,
        next_attempt_at=PAST,
        event_type="article.acquired",
        attempt_count=attempt,
    )
    before = await read_event(db_session, other)
    repository = OutboxDeliveryRepository(db_session)
    assert (
        await repository.claim_ready_batch(
            selection=DeliveryBatchSelection(event_type=f" {EVENT_TYPE} ", limit=10),
            lease_duration=LeaseDuration(LEASE),
        )
        == []
    )
    result = await repository.claim_ready_batch(
        selection=DeliveryBatchSelection(event_type=EVENT_TYPE, limit=10),
        lease_duration=LeaseDuration(LEASE),
    )
    assert [event.event_id for event in result] == [target]
    assert await read_event(db_session, other) == before


@pytest.mark.parametrize("limit", [10, 2, 12])
async def test_claim_batch_limit_leaves_excess_events_unchanged(db_session, limit):
    """件数上限までを確保し、残り2件にはlease設定も回数加算もしない。"""
    before = await seed_batch_events(
        db_session, event_type=EVENT_TYPE, count=limit + 2, attempt_count=0
    )
    result = await OutboxDeliveryRepository(db_session).claim_ready_batch(
        selection=DeliveryBatchSelection(event_type=EVENT_TYPE, limit=limit),
        lease_duration=LeaseDuration(LEASE),
    )
    claimed_ids = {event.event_id for event in result}
    assert len(result) == len(claimed_ids) == limit
    assert claimed_ids <= before.keys()
    remaining = before.keys() - claimed_ids
    assert len(remaining) == 2
    for event_id in remaining:
        assert await read_event(db_session, event_id) == before[event_id]
    for event_id in claimed_ids:
        saved = await read_event(db_session, event_id)
        assert saved["lease_token"] is not None
        assert saved["attempt_count"] == 1
