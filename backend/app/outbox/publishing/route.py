"""イベント固有の本文生成と配送先を明示する。"""

from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import UUID

from app.outbox.publishing.publisher import EventEnvelope


@dataclass(frozen=True, slots=True)
class EventMessage:
    """イベントIDと、変更せず送信する本文を保持する。"""

    event_id: UUID
    body: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, UUID) or not isinstance(self.body, str):
            raise TypeError("invalid EventMessage field type")


@dataclass(frozen=True, slots=True)
class EventDeliveryRoute:
    """一つのイベント種別を検証して一つのキューへ送る定義。"""

    event_type: str
    queue_url: str
    build_message: Callable[[EventEnvelope], EventMessage]

    def __post_init__(self) -> None:
        for value in (self.event_type, self.queue_url):
            if not isinstance(value, str):
                raise TypeError("route fields must be strings")
            if not value.strip():
                raise ValueError("route fields must not be blank")
        if not callable(self.build_message):
            raise TypeError("route requires a message builder")
