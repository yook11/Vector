"""確保済みイベントの送信と配信結果の確定を取りまとめる。"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import timedelta

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.assessment.events import ArticleAssessedInScope
from app.outbox.publish_errors import PublishCleanupError, PublishError
from app.outbox.publish_failure_handler import PublishFailureHandler
from app.outbox.publisher import (
    BatchPublishResult,
    EventEnvelope,
    EventPublisher,
    PublishFailed,
    PublishSucceeded,
)
from app.outbox.repository import ClaimedOutboxEvent, OutboxDeliveryRepository

logger = structlog.get_logger(__name__)


class OutboxRelay:
    """1回の実行で上限到達の停止と、最大10件の送信を行う。"""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        publisher: EventPublisher,
        failure_handler: PublishFailureHandler,
    ) -> None:
        self._session_factory = session_factory
        self._publisher = publisher
        self._failure_handler = failure_handler

    async def run_once(self) -> None:
        async with self._session_factory() as session:
            await OutboxDeliveryRepository(session).stop_deliveries_at_attempt_limit(
                event_type=ArticleAssessedInScope.EVENT_TYPE, limit=100
            )
            await session.commit()

        async with self._session_factory() as session:
            events = await OutboxDeliveryRepository(session).claim_ready_batch(
                event_type=ArticleAssessedInScope.EVENT_TYPE,
                lease_duration=timedelta(seconds=150),
                limit=10,
            )
            await session.commit()

        if not events:
            return

        envelopes = tuple(EventEnvelope.from_claimed(event) for event in events)
        batch = self._publisher.publish_batch(envelopes)
        if not isinstance(batch, BatchPublishResult):
            raise TypeError("publisher must return BatchPublishResult")
        try:
            self._validate_results(batch, events)
            for event, result in zip(events, batch.results, strict=True):
                if isinstance(result, PublishSucceeded):
                    await self._mark_published(event)
                else:
                    await self._failure_handler.handle(event=event, failure=result)
        finally:
            if isinstance(batch.cleanup_error, PublishCleanupError):
                self._record_cleanup_error(batch.cleanup_error)

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
    def _validate_results(
        batch: BatchPublishResult, events: list[ClaimedOutboxEvent]
    ) -> None:
        """全件の対応を確認してから、最初の配信結果をDBへ適用する。"""
        if not isinstance(batch.results, tuple):
            raise TypeError("publisher results must be a tuple")
        if batch.cleanup_error is not None and not isinstance(
            batch.cleanup_error, PublishCleanupError
        ):
            raise TypeError("invalid publisher cleanup error type")
        if len(batch.results) != len(events):
            raise ValueError("publisher result count must match claimed events")
        for event, result in zip(events, batch.results, strict=True):
            if not isinstance(result, PublishSucceeded | PublishFailed):
                raise TypeError("invalid publisher result type")
            if isinstance(result, PublishFailed) and not isinstance(
                result.error, PublishError
            ):
                raise TypeError("publisher failure must contain PublishError")
            if result.event_id != event.event_id:
                raise ValueError("publisher result IDs must match claimed event order")

    @staticmethod
    def _record_cleanup_error(error: PublishCleanupError) -> None:
        """終了処理の診断出力が送信結果やDB障害を上書きしないようにする。"""
        try:
            logger.warning(
                "outbox_publish_cleanup_failed",
                error_code=error.CODE,
                original_exception_type=error.original_exception_type,
            )
        except Exception:  # noqa: S110 — 診断出力を再帰させず元の結果を維持する。
            pass
