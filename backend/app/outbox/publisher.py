from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from app.outbox.publish_errors import PublishCleanupError, PublishError

if TYPE_CHECKING:
    from app.outbox.repository import ClaimedOutboxEvent


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


@dataclass(frozen=True, slots=True)
class PublishFailed:
    """イベント単位の送信失敗で、再試行や停止の判断は含めない。"""

    event_id: UUID
    error: PublishError = field(repr=False)


@dataclass(frozen=True, slots=True)
class BatchPublishResult:
    """入力順の送信結果と、それを上書きしない終了処理の診断情報。"""

    results: tuple[PublishSucceeded | PublishFailed, ...]
    cleanup_error: PublishCleanupError | None = field(default=None, repr=False)


class EventPublisher(Protocol):
    def publish_batch(self, envelopes: Sequence[EventEnvelope]) -> BatchPublishResult:
        """各イベントの受付結果を返し、呼び出し契約違反は送信前に拒否する。"""
        ...
