"""生成済み本文の入力契約を検証し、SQSへ送る単位と送れないイベントの失敗に分ける。"""

from collections.abc import Sequence
from dataclasses import dataclass

from app.aws.sqs.errors import MessageTooLargeError
from app.aws.sqs.outgoing_message import OutgoingSqsMessage, OutgoingSqsMessageBatch
from app.outbox.publishing.error_mapping import publish_preparation_error_from_exception
from app.outbox.publishing.errors import (
    PublishEventInvalidError,
    PublishEventInvalidReason,
)
from app.outbox.publishing.publisher import PublishFailed
from app.outbox.publishing.route import EventMessage


@dataclass(frozen=True, slots=True, init=False)
class SqsMessageBatch:
    """サイズ超過などの個別失敗と、送信できるイベントとそのSQSの送信単位を保持する。"""

    inputs: tuple[EventMessage, ...]
    sendable: tuple[EventMessage, ...]
    outgoing: OutgoingSqsMessageBatch | None
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
        prepared: dict[str, OutgoingSqsMessage] = {}
        sendable = []
        failures = []
        for message in inputs:
            try:
                prepared[str(message.event_id)] = OutgoingSqsMessage(message.body)
            except MessageTooLargeError:
                failures.append(
                    PublishFailed(
                        message.event_id,
                        PublishEventInvalidError(
                            reason=PublishEventInvalidReason.MESSAGE_TOO_LARGE
                        ),
                    )
                )
            except Exception as exc:
                failures.append(
                    PublishFailed(
                        message.event_id,
                        publish_preparation_error_from_exception(exc),
                    )
                )
            else:
                sendable.append(message)
        object.__setattr__(self, "inputs", inputs)
        object.__setattr__(self, "sendable", tuple(sendable))
        object.__setattr__(
            self, "outgoing", OutgoingSqsMessageBatch(prepared) if prepared else None
        )
        object.__setattr__(self, "failures", tuple(failures))
