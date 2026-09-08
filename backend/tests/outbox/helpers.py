from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outbox_event import OutboxEvent

PAST = datetime(2000, 1, 1, tzinfo=UTC)
FUTURE = datetime(2100, 1, 1, tzinfo=UTC)


async def insert_event(
    session: AsyncSession,
    *,
    next_attempt_at: datetime,
    published_at: datetime | None = None,
    delivery_stopped_at: datetime | None = None,
    delivery_stop_reason: str | None = None,
    lease_token: UUID | None = None,
    leased_until: datetime | None = None,
    attempt_count: int = 0,
    event_type: str = "test.outbox",
    schema_version: int = 1,
    payload: dict[str, Any] | None = None,
    occurred_at: datetime = PAST,
) -> UUID:
    """判定に使う状態は呼び出し側で指定し、commitは行わない。"""
    event_id = await session.scalar(
        insert(OutboxEvent)
        .values(
            event_type=event_type,
            schema_version=schema_version,
            payload={} if payload is None else payload,
            occurred_at=occurred_at,
            next_attempt_at=next_attempt_at,
            published_at=published_at,
            delivery_stopped_at=delivery_stopped_at,
            delivery_stop_reason=delivery_stop_reason,
            lease_token=lease_token,
            leased_until=leased_until,
            attempt_count=attempt_count,
        )
        .returning(OutboxEvent.event_id)
    )
    assert event_id is not None
    return event_id


async def read_event(session: AsyncSession, event_id: UUID) -> dict[str, Any]:
    """ORMのキャッシュを使わず、保存された全列を比較用に読む。"""
    result = await session.execute(
        select(OutboxEvent.__table__).where(OutboxEvent.event_id == event_id)
    )
    return dict(result.mappings().one())


async def seed_batch_events(session, *, event_type, count, attempt_count):
    """件数境界を検証するイベント群と、更新前の状態を用意する。"""
    for _ in range(count):
        await insert_event(
            session,
            next_attempt_at=PAST,
            event_type=event_type,
            attempt_count=attempt_count,
        )
    rows = await session.execute(select(OutboxEvent.__table__))
    return {row["event_id"]: dict(row) for row in rows.mappings()}
