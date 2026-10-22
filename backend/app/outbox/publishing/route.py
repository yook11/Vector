"""送信する本文の値と、イベント種別ごとの配送先の対応を定義する。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from app.shared.errors import ApplicationError

if TYPE_CHECKING:
    # PublishFailureがこの例外を参照するため、実行時はimportしない。
    from app.outbox.publishing.contracts import EventEnvelope


class EventRouteMismatchError(ApplicationError):
    """保存されたイベントの種別が、配送定義の種別と一致しない。"""

    def __init__(self) -> None:
        super().__init__("イベントの種別が配送定義の種別と一致しません")


@dataclass(frozen=True, slots=True)
class EventMessage:
    """イベントIDと、変更せず送信する本文を保持する。"""

    event_id: UUID
    body: str

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, UUID) or not isinstance(self.body, str):
            raise TypeError("invalid EventMessage field type")


@dataclass(frozen=True, slots=True)
class EventDeliveryRoute:
    """一つのイベント種別を一つのキューへ送る対応を表す。"""

    event_type: str
    queue_url: str

    def __post_init__(self) -> None:
        for value in (self.event_type, self.queue_url):
            if not isinstance(value, str):
                raise TypeError("route fields must be strings")
            if not value.strip():
                raise ValueError("route fields must not be blank")

    def ensure_matches(self, envelope: EventEnvelope) -> None:
        """保存されたイベントの種別が、この対応の種別と一致することを確かめる。"""
        if envelope.event_type != self.event_type:
            raise EventRouteMismatchError()
