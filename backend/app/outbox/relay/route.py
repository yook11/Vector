"""イベント種別ごとの配送先の対応を定義する。"""

from __future__ import annotations

from dataclasses import dataclass


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
