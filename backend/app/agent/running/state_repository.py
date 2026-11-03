"""Runの行をロックしてから状態を判定し、許された操作だけを書き込む。"""

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
from app.agent.running.state import (
    AnswerGenerationStarted,
    AttemptStarted,
    RunFinished,
    RunPhase,
    RunRejected,
    RunRejectionReason,
    RunState,
)
from app.agent.running.types import AgentRunErrorCode, AgentRunStatus
from app.agent.threads.citation_integrity import warn_on_citation_source_mismatch
from app.agent.threads.result_mapper import (
    build_assistant_message_for_result,
    build_source_rows_for_message,
)
from app.models.agent_message import AgentMessage
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread

__all__ = ["AgentRunStateRepository"]

_LOCK_TIMEOUT = "3s"
_NOT_FOUND = RunRejected(
    reason=RunRejectionReason.RUN_NOT_FOUND,
    observed_status=None,
)


@dataclass(frozen=True, slots=True)
class _LockedRun:
    run: AgentRun
    thread: AgentThread
    state: RunState
    now: datetime


class AgentRunStateRepository:
    """トランザクションは呼び出し元が所有し、終了通知はcommit後に呼び出し元が送る。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def start_attempt(
        self,
        run_id: uuid_mod.UUID,
        *,
        now: datetime | None = None,
    ) -> AttemptStarted | RunFinished | RunRejected:
        locked = await self._lock(run_id, now=now)
        if locked is None:
            return _NOT_FOUND
        stopped = await self._stop_if_rejected(
            locked, locked.state.check_start(now=locked.now)
        )
        if stopped is not None:
            return stopped
        attempt_epoch = locked.run.attempt_epoch + 1
        await self._update_run(
            run_id,
            status=AgentRunStatus.RUNNING.value,
            attempt_epoch=attempt_epoch,
        )
        return AttemptStarted(attempt_epoch=attempt_epoch)

    async def check_continuation(
        self,
        run_id: uuid_mod.UUID,
        *,
        attempt_epoch: int,
        now: datetime | None = None,
    ) -> RunFinished | RunRejected | None:
        locked = await self._lock(run_id, now=now)
        if locked is None:
            return _NOT_FOUND
        return await self._stop_if_rejected(
            locked,
            locked.state.check_progress(
                attempt_epoch=attempt_epoch,
                phases={RunPhase.PREPARING, RunPhase.ANSWERING},
                now=locked.now,
            ),
        )

    async def start_answer_generation(
        self,
        run_id: uuid_mod.UUID,
        *,
        attempt_epoch: int,
        now: datetime | None = None,
    ) -> AnswerGenerationStarted | RunFinished | RunRejected:
        locked = await self._lock(run_id, now=now)
        if locked is None:
            return _NOT_FOUND
        stopped = await self._stop_if_rejected(
            locked,
            locked.state.check_progress(
                attempt_epoch=attempt_epoch,
                phases={RunPhase.PREPARING},
                now=locked.now,
            ),
        )
        if stopped is not None:
            return stopped
        await self._update_run(run_id, answer_started_at=locked.now)
        return AnswerGenerationStarted(run_id=run_id, answer_started_at=locked.now)

    async def authorize_answer_regeneration(
        self,
        run_id: uuid_mod.UUID,
        *,
        attempt_epoch: int,
        now: datetime | None = None,
    ) -> RunFinished | RunRejected | None:
        locked = await self._lock(run_id, now=now)
        if locked is None:
            return _NOT_FOUND
        return await self._stop_if_rejected(
            locked,
            locked.state.check_progress(
                attempt_epoch=attempt_epoch,
                phases={RunPhase.ANSWERING},
                now=locked.now,
            ),
        )

    async def complete(
        self,
        run_id: uuid_mod.UUID,
        *,
        attempt_epoch: int,
        result: AnswerQuestionResult,
        research_handoff: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> RunFinished | RunRejected:
        locked = await self._lock(run_id, lock_thread=True, now=now)
        if locked is None:
            return _NOT_FOUND
        stopped = await self._stop_if_rejected(
            locked,
            locked.state.check_attempt_finish(
                attempt_epoch=attempt_epoch,
                phases={RunPhase.ANSWERING},
                now=locked.now,
            ),
        )
        if stopped is not None:
            return stopped

        thread = locked.thread
        assistant_message = build_assistant_message_for_result(
            thread_id=thread.id,
            seq=await self._next_message_seq(thread.id),
            result=result,
        )
        self._session.add(assistant_message)
        await self._session.flush()
        source_rows = build_source_rows_for_message(assistant_message, result)
        warn_on_citation_source_mismatch(
            run_id=run_id,
            answer=result.answer,
            source_refs=[row.source_ref for row in source_rows],
        )
        self._session.add_all(source_rows)
        await self._session.flush()

        finished = await self._finish(
            locked,
            status=AgentRunStatus.COMPLETED,
            assistant_message_id=assistant_message.id,
        )
        thread.updated_at = locked.now
        # Noneは「記録を追加しなかったRun」であり、既存handoffを消さない。
        if research_handoff is not None:
            thread.research_handoff = research_handoff
        return finished

    async def record_failure(
        self,
        run_id: uuid_mod.UUID,
        *,
        attempt_epoch: int,
        error_code: AgentRunErrorCode,
        now: datetime | None = None,
    ) -> RunFinished | RunRejected:
        locked = await self._lock(run_id, now=now)
        if locked is None:
            return _NOT_FOUND
        stopped = await self._stop_if_rejected(
            locked,
            locked.state.check_attempt_finish(
                attempt_epoch=attempt_epoch,
                phases={RunPhase.PREPARING, RunPhase.ANSWERING},
                now=locked.now,
            ),
        )
        if stopped is not None:
            return stopped
        return await self._finish(
            locked, status=AgentRunStatus.FAILED, error_code=error_code
        )

    async def record_policy_block(
        self,
        run_id: uuid_mod.UUID,
        *,
        attempt_epoch: int,
        now: datetime | None = None,
    ) -> RunFinished | RunRejected:
        locked = await self._lock(run_id, now=now)
        if locked is None:
            return _NOT_FOUND
        stopped = await self._stop_if_rejected(
            locked,
            locked.state.check_attempt_finish(
                attempt_epoch=attempt_epoch,
                phases={RunPhase.PREPARING, RunPhase.ANSWERING},
                now=locked.now,
            ),
        )
        if stopped is not None:
            return stopped
        return await self._finish(locked, status=AgentRunStatus.POLICY_BLOCKED)

    async def cancel_for_user(
        self,
        run_id: uuid_mod.UUID,
        *,
        user_id: uuid_mod.UUID,
        now: datetime | None = None,
    ) -> RunFinished | RunRejected:
        locked = await self._lock(run_id, owner_id=user_id, now=now)
        if locked is None:
            return _NOT_FOUND
        stopped = await self._stop_if_rejected(
            locked,
            locked.state.check_finish(
                phases={RunPhase.QUEUED, RunPhase.PREPARING, RunPhase.ANSWERING},
                now=locked.now,
            ),
        )
        if stopped is not None:
            return stopped
        return await self._finish(
            locked,
            status=AgentRunStatus.FAILED,
            error_code=AgentRunErrorCode.CANCELLED,
        )

    async def record_enqueue_failure(
        self,
        run_id: uuid_mod.UUID,
        *,
        now: datetime | None = None,
    ) -> RunFinished | RunRejected:
        locked = await self._lock(run_id, now=now)
        if locked is None:
            return _NOT_FOUND
        stopped = await self._stop_if_rejected(
            locked,
            locked.state.check_finish(phases={RunPhase.QUEUED}, now=locked.now),
        )
        if stopped is not None:
            return stopped
        return await self._finish(
            locked,
            status=AgentRunStatus.FAILED,
            error_code=AgentRunErrorCode.ENQUEUE_FAILED,
        )

    async def _lock(
        self,
        run_id: uuid_mod.UUID,
        *,
        owner_id: uuid_mod.UUID | None = None,
        lock_thread: bool = False,
        now: datetime | None,
    ) -> _LockedRun | None:
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
            return None
        run, thread = row
        # ロック待ちの間に期限を越えうるため、時刻はロックを取った後に読む。
        locked_now = await database_now(self._session, now)
        return _LockedRun(
            run=run,
            thread=thread,
            state=RunState(
                status=AgentRunStatus(run.status),
                attempt_epoch=run.attempt_epoch,
                deadline_at=run.deadline_at,
                answer_started_at=run.answer_started_at,
            ),
            now=locked_now,
        )

    async def _stop_if_rejected(
        self,
        locked: _LockedRun,
        reason: RunRejectionReason | None,
    ) -> RunFinished | RunRejected | None:
        if reason is None:
            return None
        if reason is RunRejectionReason.DEADLINE_EXCEEDED:
            return await self._finish(locked, status=AgentRunStatus.DEADLINE_EXCEEDED)
        return RunRejected(reason=reason, observed_status=locked.state.status)

    async def _finish(
        self,
        locked: _LockedRun,
        *,
        status: AgentRunStatus,
        error_code: AgentRunErrorCode | None = None,
        assistant_message_id: uuid_mod.UUID | None = None,
    ) -> RunFinished:
        # APIロールが更新できる列はstatusとerror_codeだけなので、変わる列だけを書く。
        values: dict[str, object] = {"status": status.value}
        if error_code is not None:
            values["error_code"] = error_code.value
        if assistant_message_id is not None:
            values["assistant_message_id"] = assistant_message_id
        await self._update_run(locked.run.id, **values)

        if locked.state.phase is RunPhase.QUEUED:
            return RunFinished(
                status=status,
                error_code=error_code,
                quota_release_outcome=await release_daily_quota(
                    self._session,
                    user_id=locked.thread.user_id,
                    usage_date=locked.run.quota_usage_date,
                ),
                running_attempt_epoch=None,
            )
        return RunFinished(
            status=status,
            error_code=error_code,
            quota_release_outcome=DailyQuotaReleaseOutcome.NOT_ELIGIBLE,
            running_attempt_epoch=locked.state.attempt_epoch,
        )

    async def _update_run(self, run_id: uuid_mod.UUID, **values: object) -> None:
        await self._session.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id)
            .values(**values)
            .execution_options(synchronize_session=False)
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
