"""1回のSQSリクエストで送れるメッセージのまとまりを定義する。"""

from dataclasses import dataclass

from app.outbox.sqs_message import MAX_MESSAGE_BYTES, SqsMessage

MAX_BATCH_MESSAGES = 10


@dataclass(frozen=True, slots=True)
class SqsMessageBatch:
    """件数・ID重複・合計サイズを構築時に検証した、不変の送信単位。"""

    messages: tuple[SqsMessage, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.messages, tuple) or any(
            not isinstance(message, SqsMessage) for message in self.messages
        ):
            raise TypeError("messages must be a tuple of SqsMessage")
        if not 1 <= len(self.messages) <= MAX_BATCH_MESSAGES:
            raise ValueError("batch must contain between one and ten messages")
        if len({message.event_id for message in self.messages}) != len(self.messages):
            raise ValueError("batch event IDs must be unique")
        if (
            sum(len(message.body.encode("utf-8")) for message in self.messages)
            > MAX_MESSAGE_BYTES
        ):
            raise ValueError("batch message bodies exceed the size limit")
