"""Run の状態から各操作を許すかを判定し、操作の結果を表す。"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.agent.answering import timing as answer_timing
from app.agent.running.daily_quota.release import DailyQuotaReleaseOutcome
from app.agent.running.types import AgentRunErrorCode, AgentRunStatus

__all__ = [
    "ACTIVE_STATUSES",
    "AnswerGenerationStarted",
    "AttemptStarted",
    "RunFinished",
    "RunRejected",
    "RunPhase",
    "RunRejectionReason",
    "RunState",
    "has_attempt_started",
    "is_finished",
]

ACTIVE_STATUSES = (AgentRunStatus.QUEUED, AgentRunStatus.RUNNING)


def is_finished(status: AgentRunStatus) -> bool:
    return status not in ACTIVE_STATUSES


def has_attempt_started(attempt_epoch: int) -> bool:
    """workerが一度も開始していないRunの実行世代は0のまま。"""
    return attempt_epoch >= 1


class RunRejectionReason(StrEnum):
    RUN_NOT_FOUND = "run_not_found"
    ALREADY_FINISHED = "already_finished"
    ATTEMPT_SUPERSEDED = "attempt_superseded"
    RUN_STARTED = "run_started"
    ANSWER_STARTED = "answer_started"
    ANSWER_NOT_STARTED = "answer_not_started"
    DEADLINE_EXCEEDED = "deadline_exceeded"


class RunPhase(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    ANSWERING = "answering"
    FINISHED = "finished"


_PHASE_RANK = {
    RunPhase.QUEUED: 0,
    RunPhase.PREPARING: 1,
    RunPhase.ANSWERING: 2,
}
_STARTABLE_PHASES = (RunPhase.QUEUED, RunPhase.PREPARING)


@dataclass(frozen=True, slots=True)
class RunState:
    status: AgentRunStatus
    attempt_epoch: int
    deadline_at: datetime
    answer_started_at: datetime | None

    @property
    def phase(self) -> RunPhase:
        if is_finished(self.status):
            return RunPhase.FINISHED
        if self.status is AgentRunStatus.QUEUED:
            return RunPhase.QUEUED
        if self.answer_started_at is None:
            return RunPhase.PREPARING
        return RunPhase.ANSWERING

    def check_start(self, *, now: datetime) -> RunRejectionReason | None:
        """新しい実行を始められるのは、待機中か回答前のRunを受理期限までに限る。"""
        return self._check(
            phases=_STARTABLE_PHASES,
            deadline=self.deadline_at,
            attempt_epoch=None,
            now=now,
        )

    def check_progress(
        self,
        *,
        attempt_epoch: int,
        phases: Collection[RunPhase],
        now: datetime,
    ) -> RunRejectionReason | None:
        """現在の実行が作業を始める・続けるのは、受理期限までに限る。"""
        return self._check(
            phases=phases,
            deadline=self.deadline_at,
            attempt_epoch=attempt_epoch,
            now=now,
        )

    def check_attempt_finish(
        self,
        *,
        attempt_epoch: int,
        phases: Collection[RunPhase],
        now: datetime,
    ) -> RunRejectionReason | None:
        """現在の実行がRunを終えるのは、段階で決まる回収期限までに限る。"""
        return self._check(
            phases=phases,
            deadline=self._recovery_deadline(),
            attempt_epoch=attempt_epoch,
            now=now,
        )

    def check_finish(
        self,
        *,
        phases: Collection[RunPhase],
        now: datetime,
    ) -> RunRejectionReason | None:
        """世代を持たない利用者やrouterがRunを終えるのも、回収期限までに限る。"""
        return self._check(
            phases=phases,
            deadline=self._recovery_deadline(),
            attempt_epoch=None,
            now=now,
        )

    def is_due_for_deadline_recovery(self, *, now: datetime) -> bool:
        return self.phase is not RunPhase.FINISHED and now >= self._recovery_deadline()

    def _check(
        self,
        *,
        phases: Collection[RunPhase],
        deadline: datetime,
        attempt_epoch: int | None,
        now: datetime,
    ) -> RunRejectionReason | None:
        phase = self.phase
        if phase is RunPhase.FINISHED:
            return RunRejectionReason.ALREADY_FINISHED
        if attempt_epoch is not None and not (
            self.status is AgentRunStatus.RUNNING
            and self.attempt_epoch == attempt_epoch
        ):
            return RunRejectionReason.ATTEMPT_SUPERSEDED
        # 回答中のRunを受理期限で期限切れにしないよう、段階を期限より先に見る。
        if phase not in phases:
            return _phase_rejection(phase, phases)
        if now >= deadline:
            return RunRejectionReason.DEADLINE_EXCEEDED
        return None

    def _recovery_deadline(self) -> datetime:
        if self.answer_started_at is None:
            return self.deadline_at
        return (
            self.answer_started_at + answer_timing.answer_generation_recovery_window()
        )


def _phase_rejection(
    phase: RunPhase, phases: Collection[RunPhase]
) -> RunRejectionReason:
    if all(_PHASE_RANK[phase] > _PHASE_RANK[allowed] for allowed in phases):
        if phase is RunPhase.PREPARING:
            return RunRejectionReason.RUN_STARTED
        return RunRejectionReason.ANSWER_STARTED
    if phase is RunPhase.PREPARING:
        return RunRejectionReason.ANSWER_NOT_STARTED
    # 待機中を許さない判定は実行世代を見るので、世代の確認で先に拒否される。
    raise ValueError(f"no rejection reason for {phase} outside {sorted(phases)}")


@dataclass(frozen=True, slots=True)
class AttemptStarted:
    attempt_epoch: int

    def __post_init__(self) -> None:
        if not _is_positive_epoch(self.attempt_epoch):
            raise ValueError("started attempt requires a positive attempt epoch")


@dataclass(frozen=True, slots=True)
class AnswerGenerationStarted:
    run_id: UUID
    answer_started_at: datetime


@dataclass(frozen=True, slots=True)
class RunFinished:
    """この操作でRunを終わらせた。"""

    status: AgentRunStatus
    error_code: AgentRunErrorCode | None
    quota_release_outcome: DailyQuotaReleaseOutcome
    # runningから終えたときの実行世代。終了通知の送り先になる。
    running_attempt_epoch: int | None

    def __post_init__(self) -> None:
        if not is_finished(self.status):
            raise ValueError("finished run requires a terminal status")
        if (self.status is AgentRunStatus.FAILED) != (self.error_code is not None):
            raise ValueError("only a failed run carries an error code")
        if not isinstance(self.quota_release_outcome, DailyQuotaReleaseOutcome):
            raise ValueError("finished run requires a quota release outcome")
        if self.running_attempt_epoch is None:
            return
        if not _is_positive_epoch(self.running_attempt_epoch):
            raise ValueError("running run requires a positive attempt epoch")
        if self.quota_release_outcome is not DailyQuotaReleaseOutcome.NOT_ELIGIBLE:
            raise ValueError("a run finished from running does not release quota")


@dataclass(frozen=True, slots=True)
class RunRejected:
    """判定で拒否し、Runを変更しなかった。"""

    reason: RunRejectionReason
    observed_status: AgentRunStatus | None

    def __post_init__(self) -> None:
        if self.reason is RunRejectionReason.DEADLINE_EXCEEDED:
            raise ValueError("deadline exceeded is reported as a finished run")
        if (self.reason is RunRejectionReason.RUN_NOT_FOUND) != (
            self.observed_status is None
        ):
            raise ValueError("only a missing run has no observed status")


def _is_positive_epoch(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1
