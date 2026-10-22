from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from app.analysis.assessment.events import AssessedEventInvalidError
from app.analysis.curation.events import CuratedEventInvalidError
from app.aws.sqs.errors import MessageSendError
from app.collection.article_acquisition.events import IncompleteArticleEventInvalidError
from app.collection.events import AnalyzableEventInvalidError
from app.outbox.publishing.route import EventRouteMismatchError

if TYPE_CHECKING:
    from app.outbox.delivery.repository import ClaimedOutboxEvent

# 型注釈とisinstanceの両方に使うため、TypeAliasやtype文ではなくUnionTypeの値として持つ。
PublishFailure = (
    MessageSendError
    | AnalyzableEventInvalidError
    | IncompleteArticleEventInvalidError
    | AssessedEventInvalidError
    | CuratedEventInvalidError
    | EventRouteMismatchError
)


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    """配信管理情報を含まない、保存済みイベントの送信内容。"""

    event_id: UUID
    event_type: str
    schema_version: int
    occurred_at: datetime
    payload: dict[str, Any]

    @classmethod
    def from_claimed(cls, event: ClaimedOutboxEvent) -> EventEnvelope:
        """確保結果からイベント本体を取り出し、payloadの参照を分離する。"""
        return cls(
            event_id=event.event_id,
            event_type=event.event_type,
            schema_version=event.schema_version,
            occurred_at=event.occurred_at,
            payload=deepcopy(event.payload),
        )


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


class EventPublisher(Protocol):
    def publish_batch(self, envelopes: Sequence[EventEnvelope]) -> BatchPublishResult:
        """各イベントの受付結果を返し、呼び出し契約違反は送信前に拒否する。"""
        ...
