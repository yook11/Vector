from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from app.aws.sqs.errors import MessageSendError
from app.aws.sqs.outgoing_message import AcceptedSqsMessage
from app.outbox.event.message import EventMessage


class MessageSender(Protocol):
    async def send_batch[T](
        self, sendable: Sequence[tuple[T, EventMessage]]
    ) -> list[tuple[T, AcceptedSqsMessage | MessageSendError]]:
        """組のメッセージを開いたときのキューへまとめて送り、組の対象と受付か失敗を渡した順で返す。"""
        ...


type MessageSenderFactory = Callable[[], AbstractAsyncContextManager[MessageSender]]
