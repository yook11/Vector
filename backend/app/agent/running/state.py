"""Run の状態を変える操作が返す結果の型と、保存された値を読む小さな判定。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from app.agent.running.types import AgentRunStatus, RunAttempt

__all__ = [
    "ACTIVE_STATUSES",
    "ActiveRunExists",
    "AnswerGenerationStarted",
    "AttemptStarted",
    "CanContinue",
    "CancelledByUser",
    "CannotContinue",
    "DailyLimitReached",
    "RunCreated",
    "RunFailed",
    "RunCompleted",
    "RunLeftQueuedError",
    "RunNotFound",
    "ThreadNotFound",
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
class RunCreated:
    """ユーザーの質問からRunを作り、待機中にした。"""

    thread_id: UUID
    run_id: UUID
    # 期限の確認の予約に使う、受理期限。
    deadline_at: datetime
    # 受付のログに使う、利用枠を使った日付と、その日の利用回数。
    usage_date: date
    used_count: int


@dataclass(frozen=True, slots=True)
class ThreadNotFound:
    """ユーザーのthreadとして見つからない。"""


@dataclass(frozen=True, slots=True)
class ActiveRunExists:
    """そのthreadに、動いているRun(待機中か実行中)がある。"""


@dataclass(frozen=True, slots=True)
class DailyLimitReached:
    """ユーザーの1日の利用枠が上限に達している。"""

    limit: int
    usage_date: date
    decided_at: datetime


class RunLeftQueuedError(Exception):
    """作ったRunをキューへ投入できず、失敗で終了させることもできなかった。"""


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
