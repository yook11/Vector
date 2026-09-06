from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.outbox.repository import OutboxDeliveryRepository
from tests.outbox.helpers import FUTURE, PAST, insert_event, read_event

pytestmark = pytest.mark.asyncio


async def test_mark_published_records_database_time_and_releases_lease(
    db_session: AsyncSession,
) -> None:
    """成功記録は成功日時とleaseだけを変更する。"""
    token = uuid4()
    event_id = await insert_event(
        db_session,
        next_attempt_at=PAST,
        lease_token=token,
        leased_until=FUTURE,
        attempt_count=3,
        payload={"curation_id": 123, "analyzed_article_id": 456},
    )
    before = await read_event(db_session, event_id)
    started_at = await db_session.scalar(select(func.statement_timestamp()))

    updated = await OutboxDeliveryRepository(db_session).mark_published(
        event_id=event_id, lease_token=token
    )

    finished_at = await db_session.scalar(select(func.statement_timestamp()))
    saved = await read_event(db_session, event_id)
    assert updated is True
    assert started_at is not None and finished_at is not None
    assert started_at <= saved["published_at"] <= finished_at
    assert saved == {
        **before,
        "published_at": saved["published_at"],
        "lease_token": None,
        "leased_until": None,
    }


@pytest.mark.parametrize("delay", [timedelta(0), timedelta(minutes=5)])
async def test_schedule_retry_records_database_time_plus_delay_and_releases_lease(
    db_session: AsyncSession, delay: timedelta
) -> None:
    """待ち時間ゼロも含め、再試行予約は次回日時とleaseだけを変更する。"""
    token = uuid4()
    event_id = await insert_event(
        db_session,
        next_attempt_at=PAST,
        lease_token=token,
        leased_until=FUTURE,
        attempt_count=3,
        payload={"curation_id": 123, "analyzed_article_id": 456},
    )
    before = await read_event(db_session, event_id)
    started_at = await db_session.scalar(select(func.statement_timestamp()))

    updated = await OutboxDeliveryRepository(db_session).schedule_retry(
        event_id=event_id, lease_token=token, retry_delay=delay
    )

    finished_at = await db_session.scalar(select(func.statement_timestamp()))
    saved = await read_event(db_session, event_id)
    assert updated is True
    assert started_at is not None and finished_at is not None
    assert started_at + delay <= saved["next_attempt_at"] <= finished_at + delay
    assert saved == {
        **before,
        "next_attempt_at": saved["next_attempt_at"],
        "lease_token": None,
        "leased_until": None,
    }


async def test_stop_delivery_records_database_time_and_reason_and_releases_lease(
    db_session: AsyncSession,
) -> None:
    """停止理由をそのまま記録し、停止の2列とleaseだけを変更する。"""
    token = uuid4()
    reason = " custom_reason "
    event_id = await insert_event(
        db_session,
        next_attempt_at=PAST,
        lease_token=token,
        leased_until=FUTURE,
        attempt_count=3,
        payload={"curation_id": 123, "analyzed_article_id": 456},
    )
    before = await read_event(db_session, event_id)
    started_at = await db_session.scalar(select(func.statement_timestamp()))

    updated = await OutboxDeliveryRepository(db_session).stop_delivery(
        event_id=event_id, lease_token=token, reason=reason
    )

    finished_at = await db_session.scalar(select(func.statement_timestamp()))
    saved = await read_event(db_session, event_id)
    assert updated is True
    assert started_at is not None and finished_at is not None
    assert started_at <= saved["delivery_stopped_at"] <= finished_at
    assert saved == {
        **before,
        "delivery_stopped_at": saved["delivery_stopped_at"],
        "delivery_stop_reason": reason,
        "lease_token": None,
        "leased_until": None,
    }


@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        ("mark_published", {}),
        ("schedule_retry", {"retry_delay": timedelta(minutes=5)}),
        ("stop_delivery", {"reason": "max_attempts"}),
    ],
)
@pytest.mark.parametrize(
    ("lease_expires_at", "published_at", "stopped_at", "stop_reason", "wrong_token"),
    [
        pytest.param(FUTURE, None, None, None, True, id="wrong-token"),
        pytest.param(PAST, None, None, None, False, id="expired-lease"),
        pytest.param(None, None, None, None, False, id="unclaimed"),
        pytest.param(FUTURE, PAST, None, None, False, id="published"),
        pytest.param(FUTURE, None, PAST, "max_attempts", False, id="stopped"),
    ],
)
async def test_delivery_update_rejects_invalid_owner_or_terminal_state(
    db_session: AsyncSession,
    operation: str,
    arguments: dict[str, Any],
    lease_expires_at: datetime | None,
    published_at: datetime | None,
    stopped_at: datetime | None,
    stop_reason: str | None,
    wrong_token: bool,
) -> None:
    """所有権や状態の条件を満たさなければ、Falseを返して全列を維持する。"""
    token = uuid4()
    event_id = await insert_event(
        db_session,
        next_attempt_at=PAST,
        lease_token=token if lease_expires_at is not None else None,
        leased_until=lease_expires_at,
        published_at=published_at,
        delivery_stopped_at=stopped_at,
        delivery_stop_reason=stop_reason,
        attempt_count=3,
    )
    before = await read_event(db_session, event_id)
    repository = OutboxDeliveryRepository(db_session)

    updated = await getattr(repository, operation)(
        event_id=event_id,
        lease_token=uuid4() if wrong_token else token,
        **arguments,
    )

    assert updated is False
    assert await read_event(db_session, event_id) == before


@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        ("mark_published", {}),
        ("schedule_retry", {"retry_delay": timedelta(minutes=5)}),
        ("stop_delivery", {"reason": "max_attempts"}),
    ],
)
async def test_delivery_update_with_missing_id_leaves_existing_event_unchanged(
    db_session: AsyncSession, operation: str, arguments: dict[str, Any]
) -> None:
    """tokenが一致しても、別のevent_idの行を更新しない。"""
    token = uuid4()
    existing_id = await insert_event(
        db_session, next_attempt_at=PAST, lease_token=token, leased_until=FUTURE
    )
    before = await read_event(db_session, existing_id)
    updated = await getattr(OutboxDeliveryRepository(db_session), operation)(
        event_id=uuid4(), lease_token=token, **arguments
    )
    assert updated is False
    assert await read_event(db_session, existing_id) == before


@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        pytest.param(
            "schedule_retry",
            {"retry_delay": timedelta(seconds=-1)},
            id="negative-delay",
        ),
        pytest.param("stop_delivery", {"reason": ""}, id="empty-reason"),
        pytest.param("stop_delivery", {"reason": " \t\n"}, id="whitespace-reason"),
    ],
)
async def test_delivery_update_rejects_invalid_arguments_without_changes(
    db_session: AsyncSession, operation: str, arguments: dict[str, Any]
) -> None:
    """更新可能な行でも、不正な待ち時間や理由は拒否する。"""
    token = uuid4()
    event_id = await insert_event(
        db_session, next_attempt_at=PAST, lease_token=token, leased_until=FUTURE
    )
    before = await read_event(db_session, event_id)
    with pytest.raises(ValueError):
        await getattr(OutboxDeliveryRepository(db_session), operation)(
            event_id=event_id, lease_token=token, **arguments
        )
    assert await read_event(db_session, event_id) == before
