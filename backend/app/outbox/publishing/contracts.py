from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.aws.sqs.errors import MessageSendError
from app.outbox.publishing.message_batch import MessageBatch
from app.outbox.publishing.route import EventRouteMismatchError
from app.shared.event_contract import EventInvalidError

# 型注釈とisinstanceの両方に使うため、TypeAliasやtype文ではなくUnionTypeの値として持つ。
PublishFailure = MessageSendError | EventInvalidError | EventRouteMismatchError


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    """配信管理情報を含まない、保存済みイベントの送信内容。"""

    event_id: UUID
    event_type: str
    schema_version: int
    occurred_at: datetime
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PublishSucceeded:
    """送信先がイベントを受け付けた結果で、consumerの完了は保証しない。"""

    event_id: UUID

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, UUID):
            raise TypeError("publish result event_id must be a UUID")


@dataclass(frozen=True, slots=True)
class PublishFailed:
    """イベント単位の送信失敗で、再試行や停止の判断は含めない。"""

    event_id: UUID
    error: PublishFailure

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, UUID):
            raise TypeError("publish result event_id must be a UUID")
        if not isinstance(self.error, PublishFailure):
            raise TypeError("publisher failure must be a PublishFailure")


class MessageSender(Protocol):
    def send_batch(
        self, *, queue_url: str, batch: MessageBatch
    ) -> tuple[PublishSucceeded | PublishFailed, ...]:
        """送信と応答検証を行い、各メッセージのイベントIDに対応する受付結果を入力順で返す。"""
        ...
