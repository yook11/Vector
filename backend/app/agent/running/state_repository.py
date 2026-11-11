"""Runの状態を変える操作が使う、条件を満たすRunへの書き込みと読み取り。"""

from __future__ import annotations

import uuid as uuid_mod
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import Row, and_, func, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.agent.answering import timing as answer_timing
from app.agent.contract import AnswerQuestionResult
from app.agent.running.daily_quota.release import release_daily_quota
from app.agent.running.state import ACTIVE_STATUSES
from app.agent.running.types import AgentRunErrorCode, AgentRunStatus, RunAttempt
from app.agent.threads.result_mapper import (
    build_assistant_message_for_result,
    build_source_rows_for_message,
)
from app.models.agent_message import AgentMessage
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread

__all__ = ["AgentRunStateRepository", "CancelledRun"]

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
class CancelledRun:
    """キャンセルで終了させたRunで、実行中だった場合だけ、画面へ終了を知らせる先の試行を持つ。"""

    running_attempt: RunAttempt | None


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

    async def end_run_of_attempt(
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
    ) -> CancelledRun | None:
        """ユーザーのキャンセルでRunを終了させる。

        次をすべて満たすRunだけが対象で、満たさなければ何も書かずNoneを返す。
        - キャンセルを実行したユーザーのRunである。
        - まだ終わっていない(待機中か実行中)。
        - 回収期限(回答前は受理期限、回答中は回答開始から数えた期限)を過ぎていない。
        """
        ended_run = await self._end_run(
            AgentRun.id == run_id,
            AgentThread.user_id == owner_id,
            AgentRun.status.in_(ACTIVE_STATUSES),
            _recovery_deadline() > self._now,
            error_code=AgentRunErrorCode.CANCELLED,
        )
        if ended_run is None:
            return None
        if ended_run.status == AgentRunStatus.QUEUED.value:
            # 処理が始まらないまま終了したので、ユーザーに利用枠を返却する。
            await release_daily_quota(
                self._session,
                user_id=ended_run.user_id,
                usage_date=ended_run.quota_usage_date,
            )
            return CancelledRun(running_attempt=None)
        return CancelledRun(
            running_attempt=RunAttempt(run_id=run_id, epoch=ended_run.attempt_epoch)
        )

    async def end_queued_run(self, run_id: uuid_mod.UUID) -> bool:
        """キューへの投入に失敗したRunを失敗で終了させ、終了させたかを返す。

        次をすべて満たすRunだけが対象で、満たさなければ何も書かずFalseを返す。
        - 待機中のままである。
        - 受理期限(受付時に決めたdeadline_at)を過ぎていない。
        """
        ended_run = await self._end_run(
            AgentRun.id == run_id,
            AgentRun.status == AgentRunStatus.QUEUED.value,
            _is_before_answer(self._now),
            error_code=AgentRunErrorCode.ENQUEUE_FAILED,
        )
        if ended_run is None:
            return False
        # 処理が始まらないまま終了したので、ユーザーに利用枠を返却する。
        await release_daily_quota(
            self._session,
            user_id=ended_run.user_id,
            usage_date=ended_run.quota_usage_date,
        )
        return True

    async def exists_for_owner(
        self, run_id: uuid_mod.UUID, *, owner_id: uuid_mod.UUID
    ) -> bool:
        found_run_id = await self._session.scalar(
            select(AgentRun.id)
            .join(AgentThread, AgentRun.thread_id == AgentThread.id)
            .where(AgentRun.id == run_id, AgentThread.user_id == owner_id)
        )
        return found_run_id is not None

    async def _end_run(
        self, *conditions: ColumnElement[bool], error_code: AgentRunErrorCode
    ) -> Row[tuple[str, int, uuid_mod.UUID, date | None]] | None:
        """Runを指定の理由(error_code)で終了させ、終了させる前の状態・世代と、ユーザーID・利用枠を使った日付を返す。

        Runが条件に合わず終了させなかったときは、Noneを返す。
        """

        await self._limit_lock_wait()
        # 書き換えた行からは前の状態を読めないので、先にロックして読んだ行を書き換える。
        before = (
            select(
                AgentRun.id,
                AgentRun.status,
                AgentRun.attempt_epoch,
                AgentThread.user_id,
                AgentRun.quota_usage_date,
            )
            .join(AgentThread, AgentRun.thread_id == AgentThread.id)
            .where(*conditions)
            .with_for_update(of=AgentRun)
            .cte("before")
        )

        return (
            await self._session.execute(
                update(AgentRun)
                .where(AgentRun.id == before.c.id)
                .values(status=AgentRunStatus.FAILED.value, error_code=error_code.value)
                .returning(
                    before.c.status,
                    before.c.attempt_epoch,
                    before.c.user_id,
                    before.c.quota_usage_date,
                )
                .execution_options(synchronize_session=False)
            )
        ).one_or_none()

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
