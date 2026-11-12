"""Run の状態を変える操作が返す結果の型と、保存された値を読む小さな判定。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

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
