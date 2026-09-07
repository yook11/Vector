"""配信失敗の判断をイベント単位でDBに確定し、結果を返す。"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import timedelta
from random import random

from sqlalchemy.ext.asyncio import AsyncSession

from app.outbox.publish_errors import PublishError
from app.outbox.publish_failure_policy import (
    NonRetryableReason,
    RetryPublish,
    decide_publish_failure,
)
from app.outbox.repository import ClaimedOutboxEvent, OutboxDeliveryRepository


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


class PublishFailureHandler:
    """失敗への対応を保存し、sessionの終了後にだけ結果を返す。"""

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
        error: PublishError,
    ) -> RetryScheduled | DeliveryStopped | DeliveryUpdateSkipped:
        """確保済みイベントの再試行予約または停止を確定する。"""
        decision = decide_publish_failure(
            error, attempt_count=event.attempt_count, jitter=self._jitter()
        )
        async with self._session_factory() as session:
            repository = OutboxDeliveryRepository(session)
            outcome: RetryScheduled | DeliveryStopped | DeliveryUpdateSkipped
            if isinstance(decision, RetryPublish):
                updated = await repository.schedule_retry(
                    event_id=event.event_id,
                    lease_token=event.lease_token,
                    retry_delay=decision.delay,
                )
                outcome = RetryScheduled(delay=decision.delay)
            else:
                updated = await repository.stop_delivery(
                    event_id=event.event_id,
                    lease_token=event.lease_token,
                    reason=decision.reason.value,
                )
                outcome = DeliveryStopped(reason=decision.reason)
            if updated:
                await session.commit()
            else:
                await session.rollback()
                outcome = DeliveryUpdateSkipped()
        return outcome
