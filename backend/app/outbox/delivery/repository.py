from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, Interval, Update, func, literal, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outbox_event import OutboxEvent
from app.outbox.delivery.retry_policy import MAX_PUBLISH_ATTEMPTS, NonRetryableReason
from app.outbox.delivery.values import DeliveryBatchSelection, LeaseDuration, RetryDelay


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
        self, *, selection: DeliveryBatchSelection, lease_duration: LeaseDuration
    ) -> list[ClaimedOutboxEvent]:
        """配信可能なイベントを確保し、更新後の値を返す。"""
        if selection.limit <= 0:
            return []

        table = OutboxEvent.__table__
        now = func.statement_timestamp(type_=DateTime(timezone=True))
        candidates = (
            select(table.c.event_id)
            .where(
                table.c.event_type == selection.event_type,
                table.c.attempt_count < MAX_PUBLISH_ATTEMPTS,
                table.c.published_at.is_(None),
                table.c.delivery_stopped_at.is_(None),
                table.c.next_attempt_at <= now,
                or_(table.c.leased_until.is_(None), table.c.leased_until <= now),
            )
            .order_by(table.c.occurred_at)
            .limit(selection.limit)
            .with_for_update(skip_locked=True)
            .cte("delivery_candidates")
        )
        statement = (
            update(table)
            .where(table.c.event_id == candidates.c.event_id)
            .values(
                lease_token=func.gen_random_uuid(),
                leased_until=now + literal(lease_duration.value, type_=Interval()),
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
        rows = sorted(result.mappings(), key=lambda row: row["occurred_at"])
        return [ClaimedOutboxEvent(**row) for row in rows]

    async def stop_deliveries_at_attempt_limit(
        self, *, selection: DeliveryBatchSelection
    ) -> list[UUID]:
        """試行上限に達し担当が不在のイベントを停止し、未commitの更新結果を返す。"""
        if selection.limit <= 0:
            return []

        table = OutboxEvent.__table__
        now = func.statement_timestamp(type_=DateTime(timezone=True))
        candidates = (
            select(table.c.event_id)
            .where(
                table.c.event_type == selection.event_type,
                table.c.attempt_count >= MAX_PUBLISH_ATTEMPTS,
                table.c.published_at.is_(None),
                table.c.delivery_stopped_at.is_(None),
                or_(table.c.leased_until.is_(None), table.c.leased_until <= now),
            )
            .order_by(table.c.occurred_at)
            .limit(selection.limit)
            .with_for_update(skip_locked=True)
            .cte("exhausted_candidates")
        )
        statement = self._stop_delivery_update(
            update(table).where(table.c.event_id == candidates.c.event_id),
            reason=NonRetryableReason.RETRY_EXHAUSTED,
        ).returning(table.c.event_id, table.c.occurred_at)
        result = await self._session.execute(statement)
        rows = sorted(result.mappings(), key=lambda row: row["occurred_at"])
        return [row["event_id"] for row in rows]

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
        retry_delay: RetryDelay,
    ) -> bool:
        """DB時刻を基準に再試行を予約し、leaseを解除する。"""
        now = func.statement_timestamp(type_=DateTime(timezone=True))
        statement = self._delivery_update(
            event_id=event_id, lease_token=lease_token
        ).values(next_attempt_at=now + literal(retry_delay.value, type_=Interval()))
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def stop_delivery(
        self, *, event_id: UUID, lease_token: UUID, reason: NonRetryableReason
    ) -> bool:
        """自動配信の停止日時と理由を記録し、leaseを解除する。"""
        statement = self._stop_delivery_update(
            self._delivery_update(event_id=event_id, lease_token=lease_token),
            reason=reason,
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    @staticmethod
    def _stop_delivery_update(
        statement: Update, *, reason: NonRetryableReason
    ) -> Update:
        """各経路の対象条件を維持し、停止記録とlease解除を同じ更新に載せる。"""
        return statement.values(
            delivery_stopped_at=func.statement_timestamp(type_=DateTime(timezone=True)),
            delivery_stop_reason=reason.value,
            lease_token=None,
            leased_until=None,
        )

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
