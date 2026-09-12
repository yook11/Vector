"""生成済み本文の入力契約とSQS送信サイズを検証する。"""

from collections.abc import Sequence
from dataclasses import dataclass

from app.outbox.publishing.error_mapping import publish_preparation_error_from_exception
from app.outbox.publishing.publisher import PublishFailed
from app.outbox.publishing.route import EventMessage
from app.outbox.sqs.message import MAX_MESSAGE_BYTES, SqsMessage


@dataclass(frozen=True, slots=True, init=False)
class SqsMessageBatch:
    """サイズ超過の個別失敗と、送信できる本文を保持する。"""

    inputs: tuple[EventMessage, ...]
    messages: tuple[SqsMessage, ...]
    failures: tuple[PublishFailed, ...]

    def __init__(self, messages: Sequence[EventMessage]) -> None:
        if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
            raise TypeError("messages must be a sequence of EventMessage")
        inputs = tuple(messages)
        if not 1 <= len(inputs) <= 10:
            raise ValueError("batch must contain between one and ten messages")
        if any(not isinstance(message, EventMessage) for message in inputs):
            raise TypeError("invalid EventMessage")
        if len({message.event_id for message in inputs}) != len(inputs):
            raise ValueError("batch message IDs must be unique")
        prepared = []
        failures = []
        for message in inputs:
            try:
                prepared.append(SqsMessage.from_message(message))
            except Exception as exc:
                failures.append(
                    PublishFailed(
                        message.event_id,
                        publish_preparation_error_from_exception(exc),
                    )
                )
        if (
            sum(len(message.body.encode("utf-8")) for message in prepared)
            > MAX_MESSAGE_BYTES
        ):
            raise ValueError("batch message bodies exceed the size limit")
        object.__setattr__(self, "inputs", inputs)
        object.__setattr__(self, "messages", tuple(prepared))
        object.__setattr__(self, "failures", tuple(failures))
