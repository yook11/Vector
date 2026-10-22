"""確保済みイベントの送信と配信結果の確定を取りまとめる。"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.outbox.delivery.failure_handler import OutboxDeliveryFailureHandler
from app.outbox.delivery.repository import LeasedOutboxEvent, OutboxDeliveryRepository
from app.outbox.delivery.values import LeaseDuration
from app.outbox.publishing.publisher import (
    BatchPublishResult,
    EventPublisher,
    PublishSucceeded,
)


class OutboxRelay:
    """1回の実行で上限到達の停止と、最大10件の送信を行う。"""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        publisher: EventPublisher,
        failure_handler: OutboxDeliveryFailureHandler,
        *,
        event_type: str,
    ) -> None:
        if not isinstance(event_type, str) or not event_type.strip():
            raise ValueError("event type must not be blank")
        self._event_type = event_type
        self._session_factory = session_factory
        self._publisher = publisher
        self._failure_handler = failure_handler

    async def run_once(self) -> None:
        async with self._session_factory() as session:
            # 試行上限に達したイベントを配信停止にする。
            await OutboxDeliveryRepository(session).stop_deliveries_at_attempt_limit(
                event_type=self._event_type, limit=100
            )
            await session.commit()

        async with self._session_factory() as session:
            repository = OutboxDeliveryRepository(session)
            leased_events = await repository.lease_events_to_publish(
                event_type=self._event_type,
                limit=10,
                lease_duration=LeaseDuration(timedelta(seconds=150)),
            )
            await session.commit()

        if not leased_events:
            return

        batch = self._publisher.publish_batch(
            tuple(leased_event.envelope for leased_event in leased_events)
        )

        if not isinstance(batch, BatchPublishResult):
            raise TypeError("publisher must return BatchPublishResult")
        self._validate_result_correspondence(batch, leased_events)
        for leased_event, result in zip(leased_events, batch.results, strict=True):
            if isinstance(result, PublishSucceeded):
                await self._mark_published(leased_event)
            else:
                await self._failure_handler.handle(
                    leased_event=leased_event, failure=result
                )

    async def _mark_published(self, leased_event: LeasedOutboxEvent) -> None:
        async with self._session_factory() as session:
            updated = await OutboxDeliveryRepository(session).mark_published(
                event_id=leased_event.envelope.event_id,
                lease_token=leased_event.lease_token,
            )
            if updated:
                await session.commit()
            else:
                await session.rollback()

    @staticmethod
    def _validate_result_correspondence(
        batch: BatchPublishResult, leased_events: list[LeasedOutboxEvent]
    ) -> None:
        """全件の対応を確認してから、最初の配信結果をDBへ適用する。"""
        if len(batch.results) != len(leased_events):
            raise ValueError("publisher result count must match leased events")
        for leased_event, result in zip(leased_events, batch.results, strict=True):
            if result.event_id != leased_event.envelope.event_id:
                raise ValueError("publisher result IDs must match leased event order")
