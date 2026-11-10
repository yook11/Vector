"""Run の状態から各操作を許すかを判定し、何が起きたかを型で返す。"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import assert_never
from uuid import UUID

from app.agent.answering import timing as answer_timing
from app.agent.running.types import AgentRunErrorCode, AgentRunStatus

__all__ = [
    "ACTIVE_STATUSES",
    "AnswerGenerationStarted",
    "AttemptAlreadyStarted",
    "AttemptStarted",
    "AttemptSuperseded",
    "CanContinue",
    "CancelledByUser",
    "CannotContinue",
    "DeadlineExceeded",
    "FailureRecorded",
    "RunAlreadyCompleted",
    "RunAlreadyFailed",
    "RunCompleted",
    "RunNotFound",
    "RunPhase",
    "RunPhaseMismatchError",
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


class RunPhase(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    ANSWERING = "answering"
    FINISHED = "finished"


_STARTABLE_PHASES = (RunPhase.QUEUED, RunPhase.PREPARING)


@dataclass(frozen=True, slots=True)
class CanContinue:
    """判定が操作を許した。"""


@dataclass(frozen=True, slots=True)
class CannotContinue:
    """現在の試行が続けられないことを、理由ごとの子の型で表す。"""


@dataclass(frozen=True, slots=True)
class CancelledByUser(CannotContinue):
    """利用者がRunを止めた。"""


@dataclass(frozen=True, slots=True)
class DeadlineExceeded(CannotContinue):
    """Runが期限を過ぎた。"""


@dataclass(frozen=True, slots=True)
class AttemptSuperseded(CannotContinue):
    """別の試行がRunを担当している、または既に終えている。"""


@dataclass(frozen=True, slots=True)
class RunNotFound(CannotContinue):
    """Runの行が無い。"""


@dataclass(frozen=True, slots=True)
class RunAlreadyCompleted:
    """Runは既に完了している。"""


@dataclass(frozen=True, slots=True)
class RunAlreadyFailed:
    """Runは既に失敗で終わっている。"""


@dataclass(frozen=True, slots=True)
class AttemptAlreadyStarted:
    """workerが既に試行を始めている。"""


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
class RunCompleted:
    """この試行の回答を保存し、Runを完了した。"""


@dataclass(frozen=True, slots=True)
class FailureRecorded:
    """Runを失敗として記録した。"""


class RunPhaseMismatchError(Exception):
    """現在の試行が許されない段階で操作した。呼び出し順の誤りを表す。"""

    def __init__(self, *, phase: RunPhase, allowed: Collection[RunPhase]) -> None:
        super().__init__(f"{phase} is outside {sorted(allowed)}")
        self.phase = phase
        self.allowed = frozenset(allowed)


type _AttemptCheck = (
    CanContinue | CancelledByUser | DeadlineExceeded | AttemptSuperseded
)
type _FinishCheck = (
    CanContinue
    | CancelledByUser
    | DeadlineExceeded
    | RunAlreadyCompleted
    | RunAlreadyFailed
)


@dataclass(frozen=True, slots=True)
class RunState:
    status: AgentRunStatus
    error_code: AgentRunErrorCode | None
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

    def check_start(self, *, now: datetime) -> _AttemptCheck:
        """新しい試行を始められるのは、待機中か回答前のRunを受理期限までに限る。"""
        phase = self.phase
        if phase is RunPhase.FINISHED:
            return self._ended_for_attempt()
        # 回答中なら前の試行が続けるので、二重に届いた依頼は期限を見ずに引き下がる。
        if phase not in _STARTABLE_PHASES:
            return AttemptSuperseded()
        return _within(self.deadline_at, now=now)

    def check_attempt(
        self,
        *,
        attempt_epoch: int,
        phases: Collection[RunPhase],
        now: datetime,
    ) -> _AttemptCheck:
        """現在の試行の操作は、回答前は受理期限まで、回答中は回収期限までに限る。"""
        phase = self.phase
        if phase is RunPhase.FINISHED:
            return self._ended_for_attempt()
        if not (
            self.status is AgentRunStatus.RUNNING
            and self.attempt_epoch == attempt_epoch
        ):
            return AttemptSuperseded()
        # 呼び出し順の誤りを期限切れとして隠さないよう、段階を期限より先に見る。
        if phase not in phases:
            raise RunPhaseMismatchError(phase=phase, allowed=phases)
        return _within(self._recovery_deadline(), now=now)

    def check_cancel(self, *, now: datetime) -> _FinishCheck:
        """利用者がRunを止めるのは、どの段階でも回収期限までに限る。"""
        if self.phase is RunPhase.FINISHED:
            return self._ended_for_caller_without_attempt()
        return _within(self._recovery_deadline(), now=now)

    def check_enqueue_failure(
        self, *, now: datetime
    ) -> _FinishCheck | AttemptAlreadyStarted:
        """投入の失敗でRunを終えるのは、待機中のまま回収期限までに限る。"""
        phase = self.phase
        if phase is RunPhase.FINISHED:
            return self._ended_for_caller_without_attempt()
        if phase is not RunPhase.QUEUED:
            return AttemptAlreadyStarted()
        return _within(self._recovery_deadline(), now=now)

    def is_due_for_deadline_recovery(self, *, now: datetime) -> bool:
        return self.phase is not RunPhase.FINISHED and now >= self._recovery_deadline()

    def _ended_for_attempt(
        self,
    ) -> CancelledByUser | DeadlineExceeded | AttemptSuperseded:
        match self.status:
            case AgentRunStatus.FAILED if (
                self.error_code is AgentRunErrorCode.CANCELLED
            ):
                return CancelledByUser()
            case AgentRunStatus.DEADLINE_EXCEEDED:
                return DeadlineExceeded()
            # 完了・失敗で終わったRunは、この試行ではなく別の試行の担当だった。
            case (
                AgentRunStatus.COMPLETED
                | AgentRunStatus.FAILED
                | AgentRunStatus.POLICY_BLOCKED
            ):
                return AttemptSuperseded()
            case AgentRunStatus.QUEUED | AgentRunStatus.RUNNING:
                raise ValueError("an active run has not ended")
            case _:
                assert_never(self.status)

    def _ended_for_caller_without_attempt(
        self,
    ) -> CancelledByUser | DeadlineExceeded | RunAlreadyCompleted | RunAlreadyFailed:
        match self.status:
            case AgentRunStatus.FAILED if (
                self.error_code is AgentRunErrorCode.CANCELLED
            ):
                return CancelledByUser()
            case AgentRunStatus.DEADLINE_EXCEEDED:
                return DeadlineExceeded()
            case AgentRunStatus.COMPLETED:
                return RunAlreadyCompleted()
            case AgentRunStatus.FAILED | AgentRunStatus.POLICY_BLOCKED:
                return RunAlreadyFailed()
            case AgentRunStatus.QUEUED | AgentRunStatus.RUNNING:
                raise ValueError("an active run has not ended")
            case _:
                assert_never(self.status)

    def _recovery_deadline(self) -> datetime:
        if self.answer_started_at is None:
            return self.deadline_at
        return (
            self.answer_started_at + answer_timing.answer_generation_recovery_window()
        )


def _within(deadline: datetime, *, now: datetime) -> CanContinue | DeadlineExceeded:
    if now >= deadline:
        return DeadlineExceeded()
    return CanContinue()


def _is_positive_epoch(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1
