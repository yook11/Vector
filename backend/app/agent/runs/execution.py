"""1回の run 実行を続けてよいか。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

__all__ = [
    "Continue",
    "RunExecutionContinuation",
    "Stop",
    "StopReason",
]


class StopReason(StrEnum):
    DEADLINE_EXCEEDED = "deadline_exceeded"
    NOT_CURRENT = "not_current"


@dataclass(frozen=True, slots=True)
class Continue:
    pass


@dataclass(frozen=True, slots=True)
class Stop:
    reason: StopReason


class RunExecutionContinuation(Protocol):
    async def should_continue(self) -> Continue | Stop: ...
