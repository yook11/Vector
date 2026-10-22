from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.aws.sqs.errors import MessageSendError
from app.outbox.publishing.route import EventMessage, EventRouteMismatchError
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


@dataclass(frozen=True, slots=True)
class BatchPublishResult:
    """入力順のイベントごとの送信結果。"""

    results: tuple[PublishSucceeded | PublishFailed, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.results, tuple):
            raise TypeError("publisher results must be a tuple")
        if any(
            not isinstance(result, PublishSucceeded | PublishFailed)
            for result in self.results
        ):
            raise TypeError("invalid publisher result type")


class MessageSender(Protocol):
    def send_batch(
        self, *, queue_url: str, messages: Sequence[EventMessage]
    ) -> BatchPublishResult: ...


def validate_send_results(result: object, messages: Sequence[EventMessage]) -> None:
    """辞書への統合で不正な対応を隠さないよう、送信対象と結果の順序まで照合する。"""
    if not isinstance(result, BatchPublishResult):
        raise TypeError("sender must return BatchPublishResult")
    if tuple(item.event_id for item in result.results) != tuple(
        message.event_id for message in messages
    ):
        raise ValueError("sender results must match input message order")
