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


class AcquireForExecutionOutcome(StrEnum):
    ACQUIRED = "acquired"
    QUEUED_START_DEADLINE_EXPIRED = "queued_start_deadline_expired"
    IDEMPOTENT_SKIP = "idempotent_skip"


@dataclass(frozen=True, slots=True)
class AcquireForExecutionCommandOutcome:
    acquire_outcome: AcquireForExecutionOutcome
    prepared_run: PreparedAgentRun | None
    quota_release_outcome: DailyQuotaReleaseOutcome | None

    def __post_init__(self) -> None:
        if not isinstance(self.acquire_outcome, AcquireForExecutionOutcome):
            raise ValueError("invalid acquire for execution outcome")
        if self.acquire_outcome is AcquireForExecutionOutcome.ACQUIRED:
            if (
                not isinstance(self.prepared_run, PreparedAgentRun)
                or self.quota_release_outcome is not None
            ):
                raise ValueError("acquired run requires only a prepared run")
            return
        if (
            self.acquire_outcome
            is AcquireForExecutionOutcome.QUEUED_START_DEADLINE_EXPIRED
        ):
            if self.prepared_run is not None or not isinstance(
                self.quota_release_outcome, DailyQuotaReleaseOutcome
            ):
                raise ValueError("expired queued run requires only a quota outcome")
            return
        if self.prepared_run is not None or self.quota_release_outcome is not None:
            raise ValueError("idempotent skip cannot contain acquire details")


class CancelRunOutcome(StrEnum):
    CANCELLED = "cancelled"
    ALREADY_FAILED = "already_failed"
    ALREADY_COMPLETED = "already_completed"
    ALREADY_POLICY_BLOCKED = "already_policy_blocked"


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
class StaleRunningRun:
    run_id: UUID
    attempt_epoch: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.attempt_epoch, int)
            or isinstance(self.attempt_epoch, bool)
            or self.attempt_epoch < 1
        ):
            raise ValueError("stale running run requires a positive attempt epoch")


@dataclass(frozen=True, slots=True)
class StaleRunSweepResult:
    queued_terminal_count: int
    queued_quota_released_count: int
    queued_quota_not_eligible_count: int
    queued_quota_inconsistent_count: int
    running_terminal_runs: tuple[StaleRunningRun, ...]
    running_quota_reservation_count: int
    running_without_started_at_count: int

    def __post_init__(self) -> None:
        counts = (
            self.queued_terminal_count,
            self.queued_quota_released_count,
            self.queued_quota_not_eligible_count,
            self.queued_quota_inconsistent_count,
            self.running_quota_reservation_count,
            self.running_without_started_at_count,
        )
        if any(
            not isinstance(count, int) or isinstance(count, bool) or count < 0
            for count in counts
        ):
            raise ValueError("stale run sweep counts must be non-negative integers")
        if (
            self.queued_quota_released_count
            + self.queued_quota_not_eligible_count
            + self.queued_quota_inconsistent_count
            != self.queued_terminal_count
        ):
            raise ValueError("queued quota outcomes must equal terminal count")
        if not all(
            isinstance(run, StaleRunningRun) for run in self.running_terminal_runs
        ):
            raise ValueError("running terminal runs must be stale running runs")

    @property
    def total_count(self) -> int:
        return self.queued_terminal_count + len(self.running_terminal_runs)


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
