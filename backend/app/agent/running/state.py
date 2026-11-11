"""Run の状態から各操作を許すかを判定し、何が起きたかを型で返す。"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import assert_never
from uuid import UUID

from app.agent.answering import timing as answer_timing
from app.agent.running.types import AgentRunErrorCode, AgentRunStatus, RunAttempt

__all__ = [
    "ACTIVE_STATUSES",
    "AnswerAlreadyStarted",
    "AnswerGenerationStarted",
    "AttemptAlreadyStarted",
    "AttemptStarted",
    "AttemptSuperseded",
    "CanContinue",
    "CancelledByUser",
    "CannotContinue",
    "DeadlineExceeded",
    "FailureRecorded",
    "RunAlreadyCancelled",
    "RunAlreadyCompleted",
    "RunAlreadyDeadlineExceeded",
    "RunAlreadyEnded",
    "RunAlreadyFailed",
    "RunCompleted",
    "RunNotFound",
    "RunPhase",
    "RunPhaseMismatchError",
    "RunState",
    "active_run_state",
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


_STARTABLE_PHASES = (RunPhase.QUEUED, RunPhase.PREPARING)


@dataclass(frozen=True, slots=True)
class CanContinue:
    """判定が操作を許した。"""


@dataclass(frozen=True, slots=True)
class CannotContinue:
    """操作を続けられない理由を子の型で表し、名前にAlreadyの付く理由はこの操作が何も書いていない。"""


@dataclass(frozen=True, slots=True)
class DeadlineExceeded(CannotContinue):
    """この操作が期限切れに気づき、Runを閉じた。"""


@dataclass(frozen=True, slots=True)
class AttemptSuperseded(CannotContinue):
    """より新しい試行がRunを担当している。"""


@dataclass(frozen=True, slots=True)
class AnswerAlreadyStarted(CannotContinue):
    """前の試行が回答中なので、新しい試行を始めない。"""


@dataclass(frozen=True, slots=True)
class RunNotFound(CannotContinue):
    """Runの行が無い。"""


@dataclass(frozen=True, slots=True)
class RunAlreadyCompleted(CannotContinue):
    """Runは既に完了している。"""


@dataclass(frozen=True, slots=True)
class RunAlreadyFailed(CannotContinue):
    """Runは既に失敗で終わっている。"""


@dataclass(frozen=True, slots=True)
class RunAlreadyCancelled(CannotContinue):
    """Runは既に利用者が止めている。"""


@dataclass(frozen=True, slots=True)
class RunAlreadyDeadlineExceeded(CannotContinue):
    """Runは既に期限切れで閉じている。"""


@dataclass(frozen=True, slots=True)
class AttemptAlreadyStarted:
    """workerが既に試行を始めている。"""


@dataclass(frozen=True, slots=True)
class CancelledByUser:
    """この操作が利用者の停止としてRunを閉じた。"""


@dataclass(frozen=True, slots=True)
class AttemptStarted:
    attempt: RunAttempt


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


type RunAlreadyEnded = (
    RunAlreadyCompleted
    | RunAlreadyFailed
    | RunAlreadyCancelled
    | RunAlreadyDeadlineExceeded
)


@dataclass(frozen=True, slots=True)
class RunState:
    """動いているRun(待機中・実行中)の状態で、終わったRunはactive_run_stateが終わり方として返す。"""

    status: AgentRunStatus
    attempt_epoch: int
    deadline_at: datetime
    answer_started_at: datetime | None

    def __post_init__(self) -> None:
        if is_finished(self.status):
            raise ValueError("run state requires an active run")

    @property
    def phase(self) -> RunPhase:
        if self.status is AgentRunStatus.QUEUED:
            return RunPhase.QUEUED
        if self.answer_started_at is None:
            return RunPhase.PREPARING
        return RunPhase.ANSWERING

    def check_start(
        self, *, now: datetime
    ) -> CanContinue | DeadlineExceeded | AnswerAlreadyStarted:
        """新しい試行を始められるのは、待機中か回答前のRunを受理期限までに限る。"""
        # 回答中なら前の試行が続けるので、二重に届いた依頼は期限を見ずに引き下がる。
        if self.phase not in _STARTABLE_PHASES:
            return AnswerAlreadyStarted()
        return _within(self.deadline_at, now=now)

    def check_attempt(
        self,
        *,
        attempt_epoch: int,
        phases: Collection[RunPhase],
        now: datetime,
    ) -> CanContinue | DeadlineExceeded | AttemptSuperseded:
        """現在の試行の操作は、回答前は受理期限まで、回答中は回収期限までに限る。"""
        if not (
            self.status is AgentRunStatus.RUNNING
            and self.attempt_epoch == attempt_epoch
        ):
            return AttemptSuperseded()
        # 呼び出し順の誤りを期限切れとして隠さないよう、段階を期限より先に見る。
        phase = self.phase
        if phase not in phases:
            raise RunPhaseMismatchError(phase=phase, allowed=phases)
        return _within(self._recovery_deadline(), now=now)

    def check_cancel(self, *, now: datetime) -> CanContinue | DeadlineExceeded:
        """利用者がRunを止めるのは、どの段階でも回収期限までに限る。"""
        return _within(self._recovery_deadline(), now=now)

    def check_enqueue_failure(
        self, *, now: datetime
    ) -> CanContinue | DeadlineExceeded | AttemptAlreadyStarted:
        """投入の失敗でRunを終えるのは、待機中のまま回収期限までに限る。"""
        if self.phase is not RunPhase.QUEUED:
            return AttemptAlreadyStarted()
        return _within(self._recovery_deadline(), now=now)

    def is_due_for_deadline_recovery(self, *, now: datetime) -> bool:
        return now >= self._recovery_deadline()

    def _recovery_deadline(self) -> datetime:
        if self.answer_started_at is None:
            return self.deadline_at
        return (
            self.answer_started_at + answer_timing.answer_generation_recovery_window()
        )


def active_run_state(
    *,
    status: AgentRunStatus,
    error_code: AgentRunErrorCode | None,
    attempt_epoch: int,
    deadline_at: datetime,
    answer_started_at: datetime | None,
) -> RunState | RunAlreadyEnded:
    """保存された値から動いているRunの状態を返し、既に終わったRunは終わり方を返す。"""
    match status:
        case AgentRunStatus.QUEUED | AgentRunStatus.RUNNING:
            return RunState(
                status=status,
                attempt_epoch=attempt_epoch,
                deadline_at=deadline_at,
                answer_started_at=answer_started_at,
            )
        case AgentRunStatus.FAILED if error_code is AgentRunErrorCode.CANCELLED:
            return RunAlreadyCancelled()
        case AgentRunStatus.DEADLINE_EXCEEDED:
            return RunAlreadyDeadlineExceeded()
        case AgentRunStatus.COMPLETED:
            return RunAlreadyCompleted()
        case AgentRunStatus.FAILED | AgentRunStatus.POLICY_BLOCKED:
            return RunAlreadyFailed()
        case _:
            assert_never(status)


def _within(deadline: datetime, *, now: datetime) -> CanContinue | DeadlineExceeded:
    if now >= deadline:
        return DeadlineExceeded()
    return CanContinue()
