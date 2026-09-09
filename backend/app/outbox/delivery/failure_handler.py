"""送信失敗への対応をDBに確定し、停止の記録までを取りまとめる。"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import timedelta
from random import random

from sqlalchemy.ext.asyncio import AsyncSession

from app.outbox.delivery.failure_recording import record_publish_failure
from app.outbox.delivery.repository import ClaimedOutboxEvent, OutboxDeliveryRepository
from app.outbox.delivery.retry_policy import (
    NonRetryableReason,
    Retryable,
    decide_publish_retry,
)
from app.outbox.publishing.publisher import PublishFailed


@dataclass(frozen=True, slots=True)
class RetryScheduled:
    """再試行予約がDBに確定した結果。"""

    delay: timedelta


@dataclass(frozen=True, slots=True)
class DeliveryStopped:
    """自動配信の停止がDBに確定した結果。"""

    reason: NonRetryableReason


@dataclass(frozen=True, slots=True)
class DeliveryUpdateSkipped:
    """更新条件を満たさず、配信状態を変更しなかった結果。"""


class OutboxDeliveryFailureHandler:
    """分類済みの送信失敗を受け取り、状態更新と確定後の記録を行う。"""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        *,
        jitter: Callable[[], float] = random,
    ) -> None:
        self._session_factory = session_factory
        self._jitter = jitter

    async def handle(
        self,
        *,
        event: ClaimedOutboxEvent,
        failure: PublishFailed,
    ) -> RetryScheduled | DeliveryStopped | DeliveryUpdateSkipped:
        """確保済みイベントの再試行予約または停止を確定する。"""
        if not isinstance(failure, PublishFailed):
            raise TypeError("failure must be PublishFailed")
        if failure.event_id != event.event_id:
            raise ValueError("failure event_id must match the claimed event")
        decision = decide_publish_retry(
            failure.error, attempt_count=event.attempt_count, jitter=self._jitter()
        )
        async with self._session_factory() as session:
            repository = OutboxDeliveryRepository(session)
            outcome: RetryScheduled | DeliveryStopped | DeliveryUpdateSkipped
            if isinstance(decision, Retryable):
                updated = await repository.schedule_retry(
                    event_id=event.event_id,
                    lease_token=event.lease_token,
                    retry_delay=decision.delay,
                )
                outcome = RetryScheduled(delay=decision.delay.value)
            else:
                updated = await repository.stop_delivery(
                    event_id=event.event_id,
                    lease_token=event.lease_token,
                    reason=decision.reason,
                )
                outcome = DeliveryStopped(reason=decision.reason)
            if updated:
                await session.commit()
            else:
                await session.rollback()
                outcome = DeliveryUpdateSkipped()
        if isinstance(outcome, DeliveryStopped):
            record_publish_failure(
                event=event, error=failure.error, stop_reason=outcome.reason
            )
        return outcome
