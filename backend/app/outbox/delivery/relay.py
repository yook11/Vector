"""確保済みイベントの送信と配信結果の確定を取りまとめる。"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.assessment.events import ArticleAssessedInScope
from app.outbox.delivery.failure_handler import OutboxDeliveryFailureHandler
from app.outbox.delivery.repository import ClaimedOutboxEvent, OutboxDeliveryRepository
from app.outbox.delivery.values import DeliveryBatchSelection, LeaseDuration
from app.outbox.publishing.publisher import (
    BatchPublishResult,
    EventEnvelope,
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
    ) -> None:
        self._session_factory = session_factory
        self._publisher = publisher
        self._failure_handler = failure_handler

    async def run_once(self) -> None:
        async with self._session_factory() as session:
            await OutboxDeliveryRepository(session).stop_deliveries_at_attempt_limit(
                selection=DeliveryBatchSelection(
                    event_type=ArticleAssessedInScope.EVENT_TYPE, limit=100
                )
            )
            await session.commit()

        async with self._session_factory() as session:
            events = await OutboxDeliveryRepository(session).claim_ready_batch(
                selection=DeliveryBatchSelection(
                    event_type=ArticleAssessedInScope.EVENT_TYPE, limit=10
                ),
                lease_duration=LeaseDuration(timedelta(seconds=150)),
            )
            await session.commit()

        if not events:
            return

        envelopes = tuple(EventEnvelope.from_claimed(event) for event in events)
        batch = self._publisher.publish_batch(envelopes)
        if not isinstance(batch, BatchPublishResult):
            raise TypeError("publisher must return BatchPublishResult")
        self._validate_result_correspondence(batch, events)
        for event, result in zip(events, batch.results, strict=True):
            if isinstance(result, PublishSucceeded):
                await self._mark_published(event)
            else:
                await self._failure_handler.handle(event=event, failure=result)

    async def _mark_published(self, event: ClaimedOutboxEvent) -> None:
        async with self._session_factory() as session:
            updated = await OutboxDeliveryRepository(session).mark_published(
                event_id=event.event_id, lease_token=event.lease_token
            )
            if updated:
                await session.commit()
            else:
                await session.rollback()

    @staticmethod
    def _validate_result_correspondence(
        batch: BatchPublishResult, events: list[ClaimedOutboxEvent]
    ) -> None:
        """全件の対応を確認してから、最初の配信結果をDBへ適用する。"""
        if len(batch.results) != len(events):
            raise ValueError("publisher result count must match claimed events")
        for event, result in zip(events, batch.results, strict=True):
            if result.event_id != event.event_id:
                raise ValueError("publisher result IDs must match claimed event order")
