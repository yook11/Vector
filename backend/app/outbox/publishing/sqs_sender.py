"""送れるイベントの組を、SQSの送信部品へ渡せる形に対応付けて送る。"""

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from uuid import UUID

from app.aws.sqs.errors import MessageSendError
from app.aws.sqs.message_sender import (
    SqsClient,
    SqsMessageSender,
    open_sqs_message_sender,
)
from app.aws.sqs.outgoing_message import AcceptedSqsMessage
from app.outbox.publishing.route import EventMessage


class SqsEventMessageSender:
    def __init__(self, sender: SqsMessageSender) -> None:
        self._sender = sender

    async def send_batch[T](
        self, sendable: Sequence[tuple[T, EventMessage]]
    ) -> list[tuple[T, AcceptedSqsMessage | MessageSendError]]:
        bodies: dict[UUID, str] = {}
        for _, message in sendable:
            bodies[message.event_id] = message.body
        send_results = await self._sender.send_batch(bodies)

        paired: list[tuple[T, AcceptedSqsMessage | MessageSendError]] = []
        for target, message in sendable:
            paired.append((target, send_results[message.event_id]))
        return paired


@asynccontextmanager
async def open_sqs_event_message_sender(
    *, client_factory: Callable[[], SqsClient], queue_url: str
) -> AsyncIterator[SqsEventMessageSender]:
    async with open_sqs_message_sender(
        client_factory=client_factory, queue_url=queue_url
    ) as sender:
        yield SqsEventMessageSender(sender)
