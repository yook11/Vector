"""Runの状態を変える操作が使う、Runの行のロック・読み出し・書き込み。"""

from __future__ import annotations

import uuid as uuid_mod
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.contract import AnswerQuestionResult
from app.agent.running.daily_quota.release import (
    DailyQuotaReleaseOutcome,
    release_daily_quota,
)
from app.agent.running.deadline.deadline_exceeded import database_now
from app.agent.running.state import RunNotFound, RunState
from app.agent.running.types import AgentRunErrorCode, AgentRunStatus
from app.agent.threads.result_mapper import (
    build_assistant_message_for_result,
    build_source_rows_for_message,
)
from app.models.agent_message import AgentMessage
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread

__all__ = ["AgentRunStateRepository", "CurrentRun"]

_LOCK_TIMEOUT = "3s"


@dataclass(frozen=True, slots=True)
class CurrentRun:
    """ロックした時点のRunの状態と時刻で、同じ操作の中で書き込んでも変わらない。"""

    state: RunState
    now: datetime


class AgentRunStateRepository:
    """トランザクションは呼び出し元が所有し、lockで読んだRunとそのthreadへ書き、nowを渡すとDBの時刻の代わりに使う。"""

    def __init__(self, session: AsyncSession, *, now: datetime | None = None) -> None:
        self._session = session
        self._now = now
        self._locked_rows: tuple[AgentRun, AgentThread] | None = None

    async def lock(
        self,
        run_id: uuid_mod.UUID,
        *,
        owner_id: uuid_mod.UUID | None = None,
        lock_thread: bool = False,
    ) -> CurrentRun | RunNotFound:
        await self._session.execute(
            select(func.set_config("lock_timeout", _LOCK_TIMEOUT, True))
        )
        query = (
            select(AgentRun, AgentThread)
            .join(AgentThread, AgentRun.thread_id == AgentThread.id)
            .where(AgentRun.id == run_id)
            .with_for_update(of=(AgentRun, AgentThread) if lock_thread else AgentRun)
            .execution_options(populate_existing=True)
        )
        if owner_id is not None:
            query = query.where(AgentThread.user_id == owner_id)
        row = (await self._session.execute(query)).one_or_none()
        if row is None:
            return RunNotFound()
        run, thread = row
        self._locked_rows = (run, thread)
        # ロック待ちの間に期限を越えうるため、時刻はロックを取った後に読む。
        locked_now = await database_now(self._session, self._now)
        return CurrentRun(
            state=RunState(
                status=AgentRunStatus(run.status),
                error_code=(
                    None
                    if run.error_code is None
                    else AgentRunErrorCode(run.error_code)
                ),
                attempt_epoch=run.attempt_epoch,
                deadline_at=run.deadline_at,
                answer_started_at=run.answer_started_at,
            ),
            now=locked_now,
        )

    async def mark_attempt_started(self, *, attempt_epoch: int) -> None:
        await self._update_run(
            status=AgentRunStatus.RUNNING.value,
            attempt_epoch=attempt_epoch,
        )

    async def mark_answer_started(self, *, answer_started_at: datetime) -> None:
        await self._update_run(answer_started_at=answer_started_at)

    async def close(
        self,
        *,
        status: AgentRunStatus,
        error_code: AgentRunErrorCode | None = None,
        assistant_message_id: uuid_mod.UUID | None = None,
    ) -> None:
        # APIロールが更新できる列はstatusとerror_codeだけなので、変わる列だけを書く。
        values: dict[str, object] = {"status": status.value}
        if error_code is not None:
            values["error_code"] = error_code.value
        if assistant_message_id is not None:
            values["assistant_message_id"] = assistant_message_id
        await self._update_run(**values)

    async def release_daily_quota(self) -> DailyQuotaReleaseOutcome:
        run, thread = self._rows()
        return await release_daily_quota(
            self._session, user_id=thread.user_id, usage_date=run.quota_usage_date
        )

    async def add_assistant_message(
        self, *, result: AnswerQuestionResult
    ) -> uuid_mod.UUID:
        _, thread = self._rows()
        assistant_message = build_assistant_message_for_result(
            thread_id=thread.id,
            seq=await self._next_message_seq(thread.id),
            result=result,
        )
        self._session.add(assistant_message)
        await self._session.flush()
        self._session.add_all(build_source_rows_for_message(assistant_message, result))
        await self._session.flush()
        return assistant_message.id

    def touch_thread(self, *, updated_at: datetime) -> None:
        _, thread = self._rows()
        thread.updated_at = updated_at

    def replace_research_handoff(self, research_handoff: dict[str, Any]) -> None:
        _, thread = self._rows()
        thread.research_handoff = research_handoff

    async def _update_run(self, **values: object) -> None:
        run, _ = self._rows()
        await self._session.execute(
            update(AgentRun)
            .where(AgentRun.id == run.id)
            .values(**values)
            .execution_options(synchronize_session=False)
        )

    def _rows(self) -> tuple[AgentRun, AgentThread]:
        if self._locked_rows is None:
            raise RuntimeError("the run must be locked before it is written")
        return self._locked_rows

    async def _next_message_seq(self, thread_id: uuid_mod.UUID) -> int:
        value = (
            await self._session.execute(
                select(func.coalesce(func.max(AgentMessage.seq), 0) + 1).where(
                    AgentMessage.thread_id == thread_id
                )
            )
        ).scalar_one()
        return int(value)
