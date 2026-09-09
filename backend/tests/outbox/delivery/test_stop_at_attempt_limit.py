"""試行上限に達したイベントの停止条件と一括処理を検証する。"""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.outbox.delivery.repository import OutboxDeliveryRepository
from app.outbox.delivery.retry_policy import MAX_PUBLISH_ATTEMPTS, NonRetryableReason
from app.outbox.delivery.values import DeliveryBatchSelection
from tests.outbox.helpers import (
    FUTURE,
    PAST,
    insert_event,
    read_event,
    seed_batch_events,
)

pytestmark = pytest.mark.asyncio
EVENT_TYPE = "article.assessed_in_scope"


async def test_stop_selects_and_returns_events_by_occurrence_time(db_session):
    """再試行予定の順番によらず、発生日時の古いイベントから選んで返す。"""
    attempt = MAX_PUBLISH_ATTEMPTS
    events = {}
    for age in (2, 0, 1):
        events[age] = await insert_event(
            db_session,
            event_type=EVENT_TYPE,
            attempt_count=attempt,
            occurred_at=PAST + timedelta(seconds=age),
            next_attempt_at=PAST + timedelta(seconds=2 - age),
        )
    result = await OutboxDeliveryRepository(
        db_session
    ).stop_deliveries_at_attempt_limit(
        selection=DeliveryBatchSelection(event_type=EVENT_TYPE, limit=2)
    )
    ids = result
    assert ids == [events[0], events[1]]


async def test_stop_equal_occurrence_times_allow_any_event_order(db_session):
    """同時刻に発生したイベントは、IDの順序を要求せず上限件数を選べる。"""
    attempt = MAX_PUBLISH_ATTEMPTS
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
    result = await OutboxDeliveryRepository(
        db_session
    ).stop_deliveries_at_attempt_limit(
        selection=DeliveryBatchSelection(event_type=EVENT_TYPE, limit=2)
    )
    ids = result
    assert len(ids) == len(set(ids)) == 2
    assert set(ids) <= events


async def test_stop_event_type_is_exact_and_old_events_are_included(db_session):
    """発生日時で除外せず、指定タイプに完全一致する行だけを扱う。"""
    attempt = MAX_PUBLISH_ATTEMPTS
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
        await repository.stop_deliveries_at_attempt_limit(
            selection=DeliveryBatchSelection(event_type=f" {EVENT_TYPE} ", limit=100)
        )
        == []
    )
    result = await repository.stop_deliveries_at_attempt_limit(
        selection=DeliveryBatchSelection(event_type=EVENT_TYPE, limit=100)
    )
    assert result == [target]
    assert await read_event(db_session, other) == before


@pytest.mark.parametrize("limit", [0, -1])
async def test_stop_nonpositive_limit_is_noop_without_database(limit):
    """非正の件数ではDB接続を必要としない。"""
    async with AsyncSession() as session:
        assert (
            await OutboxDeliveryRepository(session).stop_deliveries_at_attempt_limit(
                selection=DeliveryBatchSelection(event_type=EVENT_TYPE, limit=limit)
            )
            == []
        )


@pytest.mark.parametrize("limit", [100, 2, 12])
async def test_stop_limit_leaves_excess_events_unchanged(db_session, limit):
    """件数上限までを停止し、残り2件のDB状態は一切変更しない。"""
    before = await seed_batch_events(
        db_session,
        event_type=EVENT_TYPE,
        count=limit + 2,
        attempt_count=MAX_PUBLISH_ATTEMPTS,
    )
    result = await OutboxDeliveryRepository(
        db_session
    ).stop_deliveries_at_attempt_limit(
        selection=DeliveryBatchSelection(event_type=EVENT_TYPE, limit=limit)
    )
    stopped_ids = set(result)
    assert len(result) == len(stopped_ids) == limit
    assert stopped_ids <= before.keys()
    remaining = before.keys() - stopped_ids
    assert len(remaining) == 2
    for event_id in remaining:
        assert await read_event(db_session, event_id) == before[event_id]
    for event_id in stopped_ids:
        saved = await read_event(db_session, event_id)
        assert saved["delivery_stopped_at"] is not None
        assert saved["delivery_stop_reason"] == NonRetryableReason.RETRY_EXHAUSTED.value


async def test_next_stop_processes_events_left_unstopped(db_session):
    """前回の停止件数を超えて残ったイベントを、次回の停止処理で扱える。"""
    before = await seed_batch_events(
        db_session, event_type=EVENT_TYPE, count=3, attempt_count=MAX_PUBLISH_ATTEMPTS
    )
    repository = OutboxDeliveryRepository(db_session)
    first = await repository.stop_deliveries_at_attempt_limit(
        selection=DeliveryBatchSelection(event_type=EVENT_TYPE, limit=1)
    )
    remaining = before.keys() - set(first)
    assert len(remaining) == 2
    second = await repository.stop_deliveries_at_attempt_limit(
        selection=DeliveryBatchSelection(event_type=EVENT_TYPE, limit=100)
    )
    assert len(second) == 2
    assert set(second) == remaining


@pytest.mark.parametrize("attempt", [0, 4, 5, 6])
@pytest.mark.parametrize("lease_state", ["none", "expired", "active"])
async def test_stop_attempt_and_lease_boundaries_ignore_retry_time(
    db_session, attempt, lease_state
):
    """上限到達かつ担当不在なら未来の再試行予定でも停止し、更新列を限定する。"""
    token = uuid4() if lease_state != "none" else None
    expiry = {"none": None, "expired": PAST, "active": FUTURE}[lease_state]
    event_id = await insert_event(
        db_session,
        next_attempt_at=FUTURE,
        event_type=EVENT_TYPE,
        attempt_count=attempt,
        lease_token=token,
        leased_until=expiry,
    )
    before = await read_event(db_session, event_id)
    repository = OutboxDeliveryRepository(db_session)
    started = await db_session.scalar(select(func.statement_timestamp()))
    result = await repository.stop_deliveries_at_attempt_limit(
        selection=DeliveryBatchSelection(event_type=EVENT_TYPE, limit=100)
    )
    finished = await db_session.scalar(select(func.statement_timestamp()))
    saved = await read_event(db_session, event_id)
    if attempt < MAX_PUBLISH_ATTEMPTS or lease_state == "active":
        assert result == []
        assert saved == before
        return
    assert result == [event_id]
    assert started <= saved["delivery_stopped_at"] <= finished
    assert saved == {
        **before,
        "delivery_stopped_at": saved["delivery_stopped_at"],
        "delivery_stop_reason": NonRetryableReason.RETRY_EXHAUSTED.value,
        "lease_token": None,
        "leased_until": None,
    }


@pytest.mark.parametrize("state", ["published", "stopped"])
async def test_stop_leaves_finalized_events_unchanged(db_session, state):
    """配信済みや別理由で停止済みのイベントを上限到達へ書き換えない。"""
    fields = (
        {"published_at": PAST}
        if state == "published"
        else {
            "delivery_stopped_at": PAST,
            "delivery_stop_reason": "non_retryable_failure",
        }
    )
    event_id = await insert_event(
        db_session,
        next_attempt_at=PAST,
        event_type=EVENT_TYPE,
        attempt_count=5,
        **fields,
    )
    before = await read_event(db_session, event_id)
    assert (
        await OutboxDeliveryRepository(db_session).stop_deliveries_at_attempt_limit(
            selection=DeliveryBatchSelection(event_type=EVENT_TYPE, limit=100)
        )
        == []
    )
    assert await read_event(db_session, event_id) == before
