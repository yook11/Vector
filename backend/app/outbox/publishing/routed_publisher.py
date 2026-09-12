"""配送定義による本文生成と、送信結果のイベント対応を取りまとめる。"""

from collections.abc import Sequence
from typing import Protocol

from app.outbox.publishing.error_mapping import publish_preparation_error_from_exception
from app.outbox.publishing.errors import (
    PublishEventInvalidError,
    PublishEventInvalidReason,
)
from app.outbox.publishing.event_batch import EventBatch
from app.outbox.publishing.publisher import (
    BatchPublishResult,
    EventEnvelope,
    PublishFailed,
)
from app.outbox.publishing.route import EventDeliveryRoute, EventMessage


class MessageSender(Protocol):
    def send_batch(
        self, *, queue_url: str, messages: Sequence[EventMessage]
    ) -> BatchPublishResult: ...


class RoutedEventPublisher:
    """生成に成功した本文だけを送り、全入力の結果を入力順で返す。"""

    def __init__(self, route: EventDeliveryRoute, sender: MessageSender) -> None:
        self._route = route
        self._sender = sender

    def publish_batch(self, envelopes: Sequence[EventEnvelope]) -> BatchPublishResult:
        events = EventBatch(envelopes)
        messages = []
        results = {}
        for envelope in events.envelopes:
            try:
                message = self._route.build_message(envelope)
                if envelope.event_type != self._route.event_type:
                    raise PublishEventInvalidError(
                        reason=PublishEventInvalidReason.UNSUPPORTED_EVENT_TYPE
                    )
                if (
                    not isinstance(message, EventMessage)
                    or message.event_id != envelope.event_id
                ):
                    raise TypeError("message must preserve input event ID")
            except Exception as exc:
                results[envelope.event_id] = PublishFailed(
                    envelope.event_id,
                    publish_preparation_error_from_exception(exc),
                )
            else:
                messages.append(message)
        if messages:
            sent = self._sender.send_batch(
                queue_url=self._route.queue_url, messages=tuple(messages)
            )
            if not isinstance(sent, BatchPublishResult):
                raise TypeError("sender must return BatchPublishResult")
            if tuple(result.event_id for result in sent.results) != tuple(
                message.event_id for message in messages
            ):
                raise ValueError("sender results must match input message order")
            results.update((result.event_id, result) for result in sent.results)
        return BatchPublishResult(
            tuple(results[event.event_id] for event in events.envelopes)
        )
