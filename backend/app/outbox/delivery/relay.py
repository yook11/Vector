"""DBのイベントの確保・メッセージ化・送信・配信結果の記録を取りまとめる。"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.aws.sqs.errors import MessageSendError
from app.aws.sqs.outgoing_message import AcceptedSqsMessage
from app.outbox.delivery.failure_handler import OutboxDeliveryFailureHandler
from app.outbox.delivery.repository import LeasedOutboxEvent, OutboxDeliveryRepository
from app.outbox.delivery.values import LeaseDuration
from app.outbox.publishing.contracts import EventEnvelope, PublishFailed
from app.outbox.publishing.preparation import build_sendable_message
from app.outbox.publishing.route import (
    EventDeliveryRoute,
    EventMessage,
    EventRouteMismatchError,
)
from app.outbox.publishing.sender import MessageSenderFactory
from app.shared.event_contract import EventInvalidError


class OutboxRelay:
    """1回の実行で最大10件の確保から配信結果の記録までを進める。"""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        sender_factory: MessageSenderFactory,
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
        self._sender_factory = sender_factory
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

        sendable: list[tuple[LeasedOutboxEvent, EventMessage]] = []
        for leased_event in leased_events:
            envelope = leased_event.envelope
            try:
                message = build_sendable_message(
                    envelope, route=self._route, build_message=self._build_message
                )
            except (EventRouteMismatchError, EventInvalidError) as error:
                await self._failure_handler.handle(
                    leased_event=leased_event,
                    failure=PublishFailed(envelope.event_id, error),
                )
                continue
            sendable.append((leased_event, message))

        if not sendable:
            return

        async with AsyncExitStack() as resources:
            try:
                sender = await resources.enter_async_context(self._sender_factory())
            except MessageSendError as error:
                # 送信者を開けない失敗は全件に共通なので、各イベントの失敗にする。
                for leased_event, _ in sendable:
                    await self._failure_handler.handle(
                        leased_event=leased_event,
                        failure=PublishFailed(leased_event.envelope.event_id, error),
                    )
                return
            send_results = await sender.send_batch(sendable)

        for leased_event, send_result in send_results:
            if isinstance(send_result, AcceptedSqsMessage):
                async with self._session_factory() as session, session.begin():
                    await OutboxDeliveryRepository(session).mark_published(
                        event_id=leased_event.envelope.event_id,
                        lease_token=leased_event.lease_token,
                    )
            else:
                await self._failure_handler.handle(
                    leased_event=leased_event,
                    failure=PublishFailed(leased_event.envelope.event_id, send_result),
                )
