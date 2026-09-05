"""回答の保存と実行完了を同じトランザクションで確定する。"""

from __future__ import annotations

import uuid as uuid_mod
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.answering import timing as answer_timing
from app.agent.contract import AnswerQuestionResult
from app.agent.running.deadline.deadline_exceeded import database_now
from app.agent.runs.types import AgentRunStatus
from app.agent.threads.citation_integrity import warn_on_citation_source_mismatch
from app.agent.threads.result_mapper import (
    build_assistant_message_for_result,
    build_source_rows_for_message,
)
from app.models.agent_message import AgentMessage
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread

_ANSWER_SAVE_LOCK_TIMEOUT = "3s"


class RunCompletionFailureReason(StrEnum):
    RUN_NOT_FOUND = "run_not_found"
    NOT_RUNNING = "not_running"
    ATTEMPT_MISMATCH = "attempt_mismatch"
    ANSWER_NOT_STARTED = "answer_not_started"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    TRANSITION_LOST = "transition_lost"


@dataclass(frozen=True, slots=True)
class RunCompletionSuccess:
    pass


@dataclass(frozen=True, slots=True)
class RunCompletionFailure:
    reason: RunCompletionFailureReason


class AgentRunCompletionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def complete_run(
        self,
        *,
        run_id: uuid_mod.UUID,
        result: AnswerQuestionResult,
        expected_attempt_epoch: int,
        research_handoff: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> RunCompletionSuccess | RunCompletionFailure:
        """期限超過はcommit、それ以外の失敗は途中の保存を含め呼び出し元でrollbackする。"""
        await self._session.execute(
            select(func.set_config("lock_timeout", _ANSWER_SAVE_LOCK_TIMEOUT, True))
        )
        row = (
            await self._session.execute(
                select(AgentRun, AgentThread)
                .join(AgentThread, AgentRun.thread_id == AgentThread.id)
                .where(AgentRun.id == run_id)
                .with_for_update(of=(AgentRun, AgentThread))
                .execution_options(populate_existing=True)
            )
        ).one_or_none()
        if row is None:
            return RunCompletionFailure(RunCompletionFailureReason.RUN_NOT_FOUND)
        run, thread = row
        if run.status != AgentRunStatus.RUNNING.value:
            return RunCompletionFailure(RunCompletionFailureReason.NOT_RUNNING)
        if run.attempt_epoch != expected_attempt_epoch:
            return RunCompletionFailure(RunCompletionFailureReason.ATTEMPT_MISMATCH)
        if run.answer_started_at is None:
            return RunCompletionFailure(RunCompletionFailureReason.ANSWER_NOT_STARTED)

        now = await database_now(self._session, now)
        recovery_deadline = (
            run.answer_started_at + answer_timing.answer_generation_recovery_window()
        )
        if now >= recovery_deadline:
            run.status = AgentRunStatus.DEADLINE_EXCEEDED.value
            run.assistant_message_id = None
            run.error_code = None
            return RunCompletionFailure(RunCompletionFailureReason.DEADLINE_EXCEEDED)

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

        update_result = await self._session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.status == AgentRunStatus.RUNNING.value,
                AgentRun.attempt_epoch == expected_attempt_epoch,
                AgentRun.answer_started_at.is_not(None),
                AgentRun.answer_started_at
                + answer_timing.answer_generation_recovery_window()
                > now,
            )
            .values(
                status=AgentRunStatus.COMPLETED.value,
                assistant_message_id=assistant_message.id,
            )
            .execution_options(synchronize_session=False)
        )
        if (update_result.rowcount or 0) != 1:
            return RunCompletionFailure(RunCompletionFailureReason.TRANSITION_LOST)
        thread.updated_at = now
        # Noneは「記録を追加しなかったRun」であり、既存handoffを消さない。
        if research_handoff is not None:
            thread.research_handoff = research_handoff
        return RunCompletionSuccess()

    async def _next_message_seq(self, thread_id: uuid_mod.UUID) -> int:
        value = (
            await self._session.execute(
                select(func.coalesce(func.max(AgentMessage.seq), 0) + 1).where(
                    AgentMessage.thread_id == thread_id
                )
            )
        ).scalar_one()
        return int(value)
