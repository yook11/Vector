"""Runの状態を変える操作が使う、条件を満たすRunへの書き込みと読み取り。"""

from __future__ import annotations

import uuid as uuid_mod
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import and_, func, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.agent.answering import timing as answer_timing
from app.agent.contract import AnswerQuestionResult
from app.agent.running.daily_quota.release import (
    DailyQuotaReleaseOutcome,
    release_daily_quota,
)
from app.agent.running.state import ACTIVE_STATUSES
from app.agent.running.types import AgentRunErrorCode, AgentRunStatus, RunAttempt
from app.agent.threads.result_mapper import (
    build_assistant_message_for_result,
    build_source_rows_for_message,
)
from app.models.agent_message import AgentMessage
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread

__all__ = ["AgentRunStateRepository", "ClosedRunFacts"]

_LOCK_TIMEOUT = "3s"


def _recovery_deadline() -> ColumnElement[datetime]:
    """Runを続けてよい期限(回収期限)で、回答を始める前は受付時に決めた受理期限、始めた後は回答開始から数えた期限になる。"""
    return func.coalesce(
        AgentRun.answer_started_at + answer_timing.answer_generation_recovery_window(),
        AgentRun.deadline_at,
    )


def _is_current_attempt(attempt: RunAttempt) -> ColumnElement[bool]:
    """Runが実行中で、実行世代がこの試行のものと合っている。"""
    return and_(
        AgentRun.id == attempt.run_id,
        AgentRun.status == AgentRunStatus.RUNNING.value,
        AgentRun.attempt_epoch == attempt.epoch,
    )


def _is_before_answer(now: ColumnElement[datetime]) -> ColumnElement[bool]:
    """回答をまだ始めておらず、受理期限を過ぎていない。"""
    return and_(AgentRun.answer_started_at.is_(None), AgentRun.deadline_at > now)


def _is_answering(now: ColumnElement[datetime]) -> ColumnElement[bool]:
    """回答を始めていて、回答開始から数えた期限を過ぎていない。"""
    return and_(AgentRun.answer_started_at.is_not(None), _recovery_deadline() > now)


@dataclass(frozen=True, slots=True)
class ClosedRunFacts:
    """取り消しか投入の失敗で終了させたRunについて、利用枠を返すか、実行中だった試行の画面へ終了を知らせるために使う値。"""

    was_queued: bool
    attempt_epoch: int
    user_id: uuid_mod.UUID
    quota_usage_date: date | None


class AgentRunStateRepository:
    """トランザクションは呼び出し元が所有し、条件を満たすRunにだけ書き、nowを渡すとDBの時刻の代わりに使う。"""

    def __init__(self, session: AsyncSession, *, now: datetime | None = None) -> None:
        self._session = session
        # 期限を過ぎていないかの判定は行をロックする前に行うので、
        # ロックを待った時間は判定に入らないことがある。
        self._now: ColumnElement[datetime] = (
            literal(now) if now is not None else func.clock_timestamp()
        )

    async def start_attempt(self, run_id: uuid_mod.UUID) -> RunAttempt | None:
        """Runに新しい実行世代を割り当てて、実行を始める。

        次をすべて満たすRunだけが対象で、満たさなければ何も書かずNoneを返す。
        - まだ終わっていない(待機中か実行中)。
        - 回答を始めていない。
        - 受理期限(受付時に決めたdeadline_at)を過ぎていない。
        """
        await self._limit_lock_wait()
        epoch = await self._session.scalar(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.status.in_(ACTIVE_STATUSES),
                _is_before_answer(self._now),
            )
            .values(
                status=AgentRunStatus.RUNNING.value,
                attempt_epoch=AgentRun.attempt_epoch + 1,
            )
            .returning(AgentRun.attempt_epoch)
            .execution_options(synchronize_session=False)
        )
        return None if epoch is None else RunAttempt(run_id=run_id, epoch=epoch)

    async def can_continue(self, attempt: RunAttempt) -> bool:
        """回答以外の工程を始める前と、回答を再生成する前に、この試行が続けてよいかを確かめる。

        次をすべて満たせばTrueを返す。
        - Runが実行中で、実行世代がこの試行のものと合っている。
        - 回収期限(回答前は受理期限、回答中は回答開始から数えた期限)を過ぎていない。
        """
        found_run_id = await self._session.scalar(
            select(AgentRun.id).where(
                _is_current_attempt(attempt), _recovery_deadline() > self._now
            )
        )
        return found_run_id is not None

    async def start_answer(self, attempt: RunAttempt) -> datetime | None:
        """回答を始めた時刻を記録して、その時刻を返す。

        次をすべて満たすRunだけが対象で、満たさなければ何も書かずNoneを返す。
        - Runが実行中で、実行世代がこの試行のものと合っている。
        - 回答をまだ始めていない。
        - 受理期限(受付時に決めたdeadline_at)を過ぎていない。
        """
        await self._limit_lock_wait()
        return await self._session.scalar(
            update(AgentRun)
            .where(_is_current_attempt(attempt), _is_before_answer(self._now))
            .values(answer_started_at=self._now)
            .returning(AgentRun.answer_started_at)
            .execution_options(synchronize_session=False)
        )

    async def complete(
        self,
        attempt: RunAttempt,
        *,
        result: AnswerQuestionResult,
        research_handoff: dict[str, Any] | None,
    ) -> bool:
        """回答を保存してRunを完了にし、保存できたかを返す。

        次をすべて満たすRunだけが対象で、満たさなければ何も書かずFalseを返す。
        - Runが実行中で、実行世代がこの試行のものと合っている。
        - 回答を始めていて、回答開始から数えた期限を過ぎていない。
        """
        await self._limit_lock_wait()
        # メッセージを先に保存するので、条件に合うRunとthreadを先にロックする。
        thread_id = await self._session.scalar(
            select(AgentRun.thread_id)
            .join(AgentThread, AgentRun.thread_id == AgentThread.id)
            .where(_is_current_attempt(attempt), _is_answering(self._now))
            .with_for_update(of=(AgentRun, AgentThread))
        )
        if thread_id is None:
            return False
        assistant_message = build_assistant_message_for_result(
            thread_id=thread_id,
            seq=await self._next_message_seq(thread_id),
            result=result,
        )
        self._session.add(assistant_message)
        await self._session.flush()
        self._session.add_all(build_source_rows_for_message(assistant_message, result))
        await self._session.flush()
        await self._session.execute(
            update(AgentRun)
            .where(AgentRun.id == attempt.run_id)
            .values(
                status=AgentRunStatus.COMPLETED.value,
                assistant_message_id=assistant_message.id,
            )
            .execution_options(synchronize_session=False)
        )
        thread_update = (
            update(AgentThread)
            .where(AgentThread.id == thread_id)
            .values(updated_at=self._now)
            .execution_options(synchronize_session=False)
        )
        # research_handoffが今回生成されていれば置き換え、無ければthreadのものを残す。
        if research_handoff is not None:
            thread_update = thread_update.values(research_handoff=research_handoff)
        await self._session.execute(thread_update)
        return True

    async def fail_attempt(
        self, attempt: RunAttempt, *, error_code: AgentRunErrorCode
    ) -> bool:
        """Runを失敗で終了させ、書けたかを返す。

        次をすべて満たすRunだけが対象で、満たさなければ何も書かずFalseを返す。
        - Runが実行中で、実行世代がこの試行のものと合っている。
        - 回収期限(回答前は受理期限、回答中は回答開始から数えた期限)を過ぎていない。
        """
        await self._limit_lock_wait()
        failed_run_id = await self._session.scalar(
            update(AgentRun)
            .where(_is_current_attempt(attempt), _recovery_deadline() > self._now)
            .values(status=AgentRunStatus.FAILED.value, error_code=error_code.value)
            .returning(AgentRun.id)
            .execution_options(synchronize_session=False)
        )
        return failed_run_id is not None

    async def cancel(
        self, run_id: uuid_mod.UUID, *, owner_id: uuid_mod.UUID
    ) -> ClosedRunFacts | None:
        """ユーザーのキャンセルでRunを終了させる。

        次をすべて満たすRunだけが対象で、満たさなければ何も書かずNoneを返す。
        - キャンセルを実行したユーザーのRunである。
        - まだ終わっていない(待機中か実行中)。
        - 回収期限(回答前は受理期限、回答中は回答開始から数えた期限)を過ぎていない。
        """
        # 終了させる前に待機中だったかは、書いた後の行から読めないので、分けて書く。
        for status in ACTIVE_STATUSES:
            closed = await self._close(
                AgentRun.id == run_id,
                AgentThread.user_id == owner_id,
                _recovery_deadline() > self._now,
                status=status,
                error_code=AgentRunErrorCode.CANCELLED,
            )
            if closed is not None:
                return closed
        return None

    async def fail_queued(self, run_id: uuid_mod.UUID) -> ClosedRunFacts | None:
        """キューへの投入に失敗したRunを、失敗で終了させる。

        次をすべて満たすRunだけが対象で、満たさなければ何も書かずNoneを返す。
        - 待機中のままである。
        - 受理期限(受付時に決めたdeadline_at)を過ぎていない。
        """
        return await self._close(
            AgentRun.id == run_id,
            _is_before_answer(self._now),
            status=AgentRunStatus.QUEUED,
            error_code=AgentRunErrorCode.ENQUEUE_FAILED,
        )

    async def release_daily_quota(
        self, *, user_id: uuid_mod.UUID, usage_date: date | None
    ) -> DailyQuotaReleaseOutcome:
        return await release_daily_quota(
            self._session, user_id=user_id, usage_date=usage_date
        )

    async def exists_for_owner(
        self, run_id: uuid_mod.UUID, *, owner_id: uuid_mod.UUID
    ) -> bool:
        found_run_id = await self._session.scalar(
            select(AgentRun.id)
            .join(AgentThread, AgentRun.thread_id == AgentThread.id)
            .where(AgentRun.id == run_id, AgentThread.user_id == owner_id)
        )
        return found_run_id is not None

    async def _close(
        self,
        *conditions: ColumnElement[bool],
        status: AgentRunStatus,
        error_code: AgentRunErrorCode,
    ) -> ClosedRunFacts | None:
        await self._limit_lock_wait()
        # APIロールが更新できる列はstatusとerror_codeだけなので、この2列だけを書く。
        row = (
            await self._session.execute(
                update(AgentRun)
                .where(
                    AgentRun.thread_id == AgentThread.id,
                    AgentRun.status == status.value,
                    *conditions,
                )
                .values(status=AgentRunStatus.FAILED.value, error_code=error_code.value)
                .returning(
                    AgentRun.attempt_epoch,
                    AgentThread.user_id,
                    AgentRun.quota_usage_date,
                )
                .execution_options(synchronize_session=False)
            )
        ).one_or_none()
        if row is None:
            return None
        return ClosedRunFacts(
            was_queued=status is AgentRunStatus.QUEUED,
            attempt_epoch=row.attempt_epoch,
            user_id=row.user_id,
            quota_usage_date=row.quota_usage_date,
        )

    async def _limit_lock_wait(self) -> None:
        """他の処理が同じ行を書き換え中のときに待つ時間を、このトランザクションだけ3秒までにする。"""
        await self._session.execute(
            select(func.set_config("lock_timeout", _LOCK_TIMEOUT, True))
        )

    async def _next_message_seq(self, thread_id: uuid_mod.UUID) -> int:
        value = (
            await self._session.execute(
                select(func.coalesce(func.max(AgentMessage.seq), 0) + 1).where(
                    AgentMessage.thread_id == thread_id
                )
            )
        ).scalar_one()
        return int(value)
