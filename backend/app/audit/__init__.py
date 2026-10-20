"""pipeline_events の stage・event_type の語彙と、失敗分類の共有部品。"""

from app.audit.domain.event import EventType, Stage

__all__ = [
    "EventType",
    "Stage",
]
