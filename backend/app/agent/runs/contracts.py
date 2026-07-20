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


class DailyQuotaReleaseOutcome(StrEnum):
    RELEASED = "released"
    NOT_ELIGIBLE = "not_eligible"
    INCONSISTENT = "inconsistent"


@dataclass(frozen=True, slots=True)
class CancelRunResult:
    outcome: CancelRunOutcome
    attempt_epoch: int | None = None
    quota_release_outcome: DailyQuotaReleaseOutcome | None = None
    quota_usage_date: date | None = None
    quota_used_count: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, CancelRunOutcome):
            raise ValueError("invalid cancel run outcome")
        if self.outcome is not CancelRunOutcome.CANCELLED:
            if any(
                value is not None
                for value in (
                    self.attempt_epoch,
                    self.quota_release_outcome,
                    self.quota_usage_date,
                    self.quota_used_count,
                )
            ):
                raise ValueError("already terminal run cannot contain cancel details")
            return

        if self.attempt_epoch is None or self.attempt_epoch < 0:
            raise ValueError("cancelled run requires a non-negative attempt epoch")
        if not isinstance(self.quota_release_outcome, DailyQuotaReleaseOutcome):
            raise ValueError("cancelled run requires a quota release outcome")
        if self.quota_release_outcome is DailyQuotaReleaseOutcome.RELEASED:
            if (
                self.quota_usage_date is None
                or self.quota_used_count is None
                or self.quota_used_count < 0
            ):
                raise ValueError(
                    "released quota requires a date and non-negative used count"
                )
            return
        if self.quota_release_outcome is DailyQuotaReleaseOutcome.INCONSISTENT:
            if self.quota_usage_date is None or self.quota_used_count is not None:
                raise ValueError(
                    "inconsistent quota release requires only the quota date"
                )
            return
        if self.quota_release_outcome is DailyQuotaReleaseOutcome.NOT_ELIGIBLE:
            if self.quota_used_count is not None:
                raise ValueError(
                    "not eligible quota release cannot contain a used count"
                )
            return
        raise ValueError("invalid quota release outcome")


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
