"""期限切れRunの回収結果。"""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class DeadlineExceededRunningRun:
    run_id: UUID
    attempt_epoch: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.attempt_epoch, int)
            or isinstance(self.attempt_epoch, bool)
            or self.attempt_epoch < 1
        ):
            raise ValueError(
                "deadline-exceeded running run requires a positive attempt epoch"
            )


@dataclass(frozen=True, slots=True)
class DeadlineRunSweepResult:
    queued_terminal_count: int
    queued_quota_released_count: int
    queued_quota_not_eligible_count: int
    queued_quota_inconsistent_count: int
    running_terminal_runs: tuple[DeadlineExceededRunningRun, ...]
    running_quota_reservation_count: int

    def __post_init__(self) -> None:
        counts = (
            self.queued_terminal_count,
            self.queued_quota_released_count,
            self.queued_quota_not_eligible_count,
            self.queued_quota_inconsistent_count,
            self.running_quota_reservation_count,
        )
        if any(
            not isinstance(count, int) or isinstance(count, bool) or count < 0
            for count in counts
        ):
            raise ValueError("deadline run sweep counts must be non-negative integers")
        if (
            self.queued_quota_released_count
            + self.queued_quota_not_eligible_count
            + self.queued_quota_inconsistent_count
            != self.queued_terminal_count
        ):
            raise ValueError("queued quota outcomes must equal terminal count")
        if not all(
            isinstance(run, DeadlineExceededRunningRun)
            for run in self.running_terminal_runs
        ):
            raise ValueError(
                "running terminal runs must be deadline-exceeded running runs"
            )

    @property
    def total_count(self) -> int:
        return self.queued_terminal_count + len(self.running_terminal_runs)
