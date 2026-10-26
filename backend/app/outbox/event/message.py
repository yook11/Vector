from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class EventMessage:
    """イベントIDと、変更せず送信する本文を保持する。"""

    event_id: UUID
    body: str

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, UUID) or not isinstance(self.body, str):
            raise TypeError("invalid EventMessage field type")
