"""run attempt の実行を止める理由。"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["StopReason"]


class StopReason(StrEnum):
    DEADLINE_EXCEEDED = "deadline_exceeded"
    NOT_CURRENT = "not_current"
