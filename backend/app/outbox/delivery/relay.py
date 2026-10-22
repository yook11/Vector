"""DBのイベントの確保・メッセージ化・送信・配信結果の記録を取りまとめる。"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.outbox.delivery.failure_handler import OutboxDeliveryFailureHandler
from app.outbox.delivery.repository import LeasedOutboxEvent, OutboxDeliveryRepository
from app.outbox.delivery.values import LeaseDuration
from app.outbox.publishing.contracts import (
    BatchPublishResult,
    EventEnvelope,
    MessageSender,
    PublishSucceeded,
    validate_send_results,
)
from app.outbox.publishing.preparation import prepare_messages
from app.outbox.publishing.route import EventDeliveryRoute, EventMessage


class OutboxRelay:
    """1回の実行で最大10件の確保から配信結果の記録までを進める。"""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        sender: MessageSender,
        failure_handler: OutboxDeliveryFailureHandler,
        *,
        route: EventDeliveryRoute,
        build_message: Callable[[EventEnvelope], EventMessage],
    ) -> None:
        if not callable(build_message):
            raise TypeError("relay requires a message builder")
        self._route = route
        self._build_message = build_message
        self._session_factory = session_factory
        self._sender = sender
        self._failure_handler = failure_handler

    async def run_once(self) -> None:
        async with self._session_factory() as session:
            # 試行上限に達したイベントを配信停止にする。
            await OutboxDeliveryRepository(session).stop_deliveries_at_attempt_limit(
                event_type=self._route.event_type, limit=100
            )
            await session.commit()

        async with self._session_factory() as session:
            repository = OutboxDeliveryRepository(session)
            leased_events = await repository.lease_events_to_publish(
                event_type=self._route.event_type,
                limit=10,
                lease_duration=LeaseDuration(timedelta(seconds=150)),
            )
            await session.commit()

        if not leased_events:
            return

        envelopes = tuple(leased_event.envelope for leased_event in leased_events)

        messages, conversion_failures = prepare_messages(
            envelopes, route=self._route, build_message=self._build_message
        )

        if messages:
            sent = self._sender.send_batch(
                queue_url=self._route.queue_url, messages=messages
            )
            validate_send_results(sent, messages)
            send_results = sent.results
        else:
            send_results = ()

        results_by_id = {
            result.event_id: result for result in (*conversion_failures, *send_results)
        }
        batch = BatchPublishResult(
            tuple(results_by_id[envelope.event_id] for envelope in envelopes)
        )

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
