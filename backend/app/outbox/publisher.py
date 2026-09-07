from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

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


class EventPublisher(Protocol):
    def publish(self, envelope: EventEnvelope) -> None:
        """送信先の受付成功時はNone、失敗時はPublishErrorを送出し、consumerの完了は保証しない。"""
        ...
