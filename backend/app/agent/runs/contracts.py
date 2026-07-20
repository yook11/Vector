"""Agent run lifecycle contracts and outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from uuid import UUID

from app.agent.runs.daily_quota.contracts import DailyQuotaReleaseOutcome
from app.agent.runs.types import AgentRunErrorCode, AgentRunStatus


class ThreadNotFoundError(Exception):
    """Requested thread is missing or not owned by the current user."""


class ActiveRunConflictError(Exception):
    """A queued/running run already exists for the requested thread."""


class RunTransitionLostError(Exception):
    """Another actor moved the run before this transition could commit."""


class CancelRunOutcome(StrEnum):
    CANCELLED = "cancelled"
    ALREADY_FAILED = "already_failed"
    ALREADY_COMPLETED = "already_completed"


@dataclass(frozen=True, slots=True)
class CancelRunCommandOutcome:
    cancel_outcome: CancelRunOutcome
    was_running: bool = False
    running_attempt_epoch: int | None = None
    quota_release_outcome: DailyQuotaReleaseOutcome | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.cancel_outcome, CancelRunOutcome):
            raise ValueError("invalid cancel run outcome")
        if not isinstance(self.was_running, bool):
            raise ValueError("was_running must be a boolean")
        if self.cancel_outcome is not CancelRunOutcome.CANCELLED:
            if (
                self.was_running
                or self.running_attempt_epoch is not None
                or self.quota_release_outcome is not None
            ):
                raise ValueError("already terminal run cannot contain cancel details")
            return

        if not isinstance(self.quota_release_outcome, DailyQuotaReleaseOutcome):
            raise ValueError("cancelled run requires a quota release outcome")
        if self.was_running:
            if (
                not isinstance(self.running_attempt_epoch, int)
                or isinstance(self.running_attempt_epoch, bool)
                or self.running_attempt_epoch < 1
            ):
                raise ValueError("running cancel requires a positive attempt epoch")
        elif self.running_attempt_epoch is not None:
            raise ValueError("queued cancel cannot contain a running attempt epoch")


@dataclass(frozen=True, slots=True)
class StaleRunSweepResult:
    total_count: int
    quota_queued_count: int
    quota_running_count: int

    def __post_init__(self) -> None:
        counts = (
            self.total_count,
            self.quota_queued_count,
            self.quota_running_count,
        )
        if any(
            not isinstance(count, int) or isinstance(count, bool) or count < 0
            for count in counts
        ):
            raise ValueError("stale run sweep counts must be non-negative integers")
        if self.quota_queued_count + self.quota_running_count > self.total_count:
            raise ValueError("quota stale run counts cannot exceed total count")


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
