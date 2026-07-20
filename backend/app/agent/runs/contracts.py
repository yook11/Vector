"""Agent run lifecycle contracts and outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from app.agent.runs.types import AgentRunErrorCode, AgentRunStatus


class ThreadNotFoundError(Exception):
    """Requested thread is missing or not owned by the current user."""


class ActiveRunConflictError(Exception):
    """A queued/running run already exists for the requested thread."""


class DailyRequestLimitExceededError(Exception):
    """The user's daily research request reservation limit was reached."""

    def __init__(
        self,
        *,
        usage_date: date,
        observed_at: datetime,
        decided_at: datetime,
        limit: int,
    ) -> None:
        super().__init__("Daily research request limit exceeded")
        self.usage_date = usage_date
        self.observed_at = observed_at
        self.decided_at = decided_at
        self.limit = limit


class RunTransitionLostError(Exception):
    """Another actor moved the run before this transition could commit."""


class CancelRunOutcome(StrEnum):
    CANCELLED = "cancelled"
    ALREADY_FAILED = "already_failed"
    ALREADY_COMPLETED = "already_completed"


@dataclass(frozen=True, slots=True)
class CancelRunResult:
    outcome: CancelRunOutcome
    attempt_epoch: int | None = None

    def __post_init__(self) -> None:
        if self.outcome is CancelRunOutcome.CANCELLED:
            if self.attempt_epoch is None or self.attempt_epoch < 0:
                raise ValueError("cancelled run requires a non-negative attempt epoch")
        elif self.attempt_epoch is not None:
            raise ValueError("attempt epoch is only valid for a cancelled run")


@dataclass(frozen=True, slots=True)
class CreatedAgentRun:
    thread_id: UUID
    run_id: UUID
    usage_date: date
    used_count: int


@dataclass(frozen=True, slots=True)
class PreparedAgentRun:
    run_id: UUID
    thread_id: UUID
    question: str
    user_message_seq: int
    attempt_epoch: int


@dataclass(frozen=True, slots=True)
class OwnedAgentRunLiveContext:
    run_id: UUID
    status: AgentRunStatus
    attempt_epoch: int
    error_code: AgentRunErrorCode | None
