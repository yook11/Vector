from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, Interval, Update, func, literal, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outbox_event import OutboxEvent


@dataclass(frozen=True, slots=True)
class ClaimedOutboxEvent:
    """確保後の値。session終了後も遅延ロードせず読める。"""

    event_id: UUID
    event_type: str
    schema_version: int
    payload: dict[str, Any]
    occurred_at: datetime
    attempt_count: int
    lease_token: UUID
    leased_until: datetime


class OutboxDeliveryRepository:
    """送信対象の確保と配信結果の記録を行う。commitは呼び出し元が管理する。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim_ready_batch(
        self, *, limit: int, lease_duration: timedelta
    ) -> list[ClaimedOutboxEvent]:
        """配信可能なイベントを確保し、更新後の値を返す。"""
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if limit <= 0:
            return []

        table = OutboxEvent.__table__
        now = func.statement_timestamp(type_=DateTime(timezone=True))
        candidates = (
            select(table.c.event_id)
            .where(
                table.c.published_at.is_(None),
                table.c.delivery_stopped_at.is_(None),
                table.c.next_attempt_at <= now,
                or_(table.c.leased_until.is_(None), table.c.leased_until <= now),
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
            .cte("delivery_candidates")
        )
        statement = (
            update(table)
            .where(table.c.event_id == candidates.c.event_id)
            .values(
                lease_token=func.gen_random_uuid(),
                leased_until=now + literal(lease_duration, type_=Interval()),
                attempt_count=table.c.attempt_count + 1,
            )
            .returning(
                table.c.event_id,
                table.c.event_type,
                table.c.schema_version,
                table.c.payload,
                table.c.occurred_at,
                table.c.attempt_count,
                table.c.lease_token,
                table.c.leased_until,
            )
        )
        result = await self._session.execute(statement)
        return [ClaimedOutboxEvent(**row) for row in result.mappings()]

    async def mark_published(self, *, event_id: UUID, lease_token: UUID) -> bool:
        """有効な担当者による送信成功を記録し、leaseを解除する。"""
        statement = self._delivery_update(
            event_id=event_id, lease_token=lease_token
        ).values(published_at=func.statement_timestamp())
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def schedule_retry(
        self,
        *,
        event_id: UUID,
        lease_token: UUID,
        retry_delay: timedelta,
    ) -> bool:
        """DB時刻を基準に再試行を予約し、leaseを解除する。"""
        if retry_delay < timedelta(0):
            raise ValueError("retry_delay must not be negative")

        now = func.statement_timestamp(type_=DateTime(timezone=True))
        statement = self._delivery_update(
            event_id=event_id, lease_token=lease_token
        ).values(next_attempt_at=now + literal(retry_delay, type_=Interval()))
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def stop_delivery(
        self, *, event_id: UUID, lease_token: UUID, reason: str
    ) -> bool:
        """自動配信の停止日時と理由を記録し、leaseを解除する。"""
        if not reason.strip():
            raise ValueError("reason must not be blank")

        statement = self._delivery_update(
            event_id=event_id, lease_token=lease_token
        ).values(
            delivery_stopped_at=func.statement_timestamp(),
            delivery_stop_reason=reason,
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    def _delivery_update(self, *, event_id: UUID, lease_token: UUID) -> Update:
        """未確定かつ有効なleaseの所有者だけが配信結果を更新できる。"""
        table = OutboxEvent.__table__
        return (
            update(table)
            .where(
                table.c.event_id == event_id,
                table.c.lease_token == lease_token,
                table.c.leased_until > func.statement_timestamp(),
                table.c.published_at.is_(None),
                table.c.delivery_stopped_at.is_(None),
            )
            .values(lease_token=None, leased_until=None)
            .returning(table.c.event_id)
        )
