"""Run の状態を変える操作が返す結果の型と、保存された値を読む小さな判定。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.agent.answering import timing as answer_timing
from app.agent.running.types import AgentRunStatus, RunAttempt

__all__ = [
    "ACTIVE_STATUSES",
    "AnswerGenerationStarted",
    "AttemptStarted",
    "CanContinue",
    "CancelledByUser",
    "CannotContinue",
    "RunFailed",
    "RunCompleted",
    "RunNotFound",
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


@dataclass(frozen=True, slots=True)
class CanContinue:
    """操作を続けてよい。"""


@dataclass(frozen=True, slots=True)
class CannotContinue:
    """Runが条件を満たさず、何も書かなかった。"""


@dataclass(frozen=True, slots=True)
class RunNotFound:
    """利用者のRunとして見つからない。"""


@dataclass(frozen=True, slots=True)
class CancelledByUser:
    """利用者の取り消しでRunを終了させた。"""


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
class RunFailed:
    """Runを失敗で終了させた。"""


@dataclass(frozen=True, slots=True)
class RunState:
    """期限の回収がロックして読んだ、動いているRun(待機中・実行中)の状態。"""

    status: AgentRunStatus
    attempt_epoch: int
    deadline_at: datetime
    answer_started_at: datetime | None

    def __post_init__(self) -> None:
        if is_finished(self.status):
            raise ValueError("run state requires an active run")

    def is_due_for_deadline_recovery(self, *, now: datetime) -> bool:
        return now >= self._recovery_deadline()

    def _recovery_deadline(self) -> datetime:
        if self.answer_started_at is None:
            return self.deadline_at
        return (
            self.answer_started_at + answer_timing.answer_generation_recovery_window()
        )
