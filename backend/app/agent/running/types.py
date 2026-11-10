"""Agent run state vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


@dataclass(frozen=True, slots=True)
class RunAttempt:
    """Runのどの試行かを、Runのidと1以上の実行世代で表す。"""

    run_id: UUID
    epoch: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.epoch, int)
            or isinstance(self.epoch, bool)
            or self.epoch < 1
        ):
            raise ValueError("run attempt requires a positive epoch")


class AgentRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    POLICY_BLOCKED = "policy_blocked"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    FAILED = "failed"


class AgentRunErrorCode(StrEnum):
    GENERATION_UNAVAILABLE = "generation_unavailable"
    INTERNAL_ERROR = "internal_error"
    ENQUEUE_FAILED = "enqueue_failed"
    STALE = "stale"
    CANCELLED = "cancelled"
