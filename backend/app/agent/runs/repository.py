"""Repository for agent run lifecycle commands and state."""

from __future__ import annotations

import uuid as uuid_mod
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.contract import AnswerQuestionResult
from app.agent.daily_quota import observability as daily_quota_observability
from app.agent.daily_quota.contracts import DailyQuotaReleaseOutcome
from app.agent.daily_quota.persistence import (
    release_daily_quota,
    reserve_daily_quota,
)
from app.agent.run_deadline.persistence import database_now, expire_run
from app.agent.run_deadline.policy import deadline_for_run
from app.agent.runs.contracts import (
    ActiveRunConflictError,
    CancelRunCommandOutcome,
    CancelRunOutcome,
    CompleteRunOutcome,
    CreatedAgentRun,
    OwnedAgentRunLiveContext,
    RunTransitionLostError,
    StartRunFailure,
    StartRunFailureReason,
    ThreadNotFoundError,
    UserQuestionMessage,
)
from app.agent.runs.execution import Continue, Stop, StopReason
from app.agent.runs.projection import build_research_run_response
from app.agent.runs.types import AgentRunErrorCode, AgentRunStatus
from app.agent.threads.citation_integrity import warn_on_citation_source_mismatch
from app.agent.threads.result_mapper import (
    build_assistant_message_for_result,
    build_source_rows_for_message,
)
from app.models.agent_message import AgentMessage
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread
from app.schemas.research import ResearchRunResponse

logger = structlog.get_logger(__name__)

_ANSWER_SAVE_LOCK_TIMEOUT = "10s"

_ACTIVE_STATUSES = (AgentRunStatus.QUEUED.value, AgentRunStatus.RUNNING.value)
_TERMINAL_STATUSES = (
    AgentRunStatus.COMPLETED.value,
    AgentRunStatus.POLICY_BLOCKED.value,
    AgentRunStatus.DEADLINE_EXCEEDED.value,
    AgentRunStatus.FAILED.value,
)


class AgentRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_user_run(
        self,
        *,
        user_id: uuid_mod.UUID,
        question: str,
        thread_id: uuid_mod.UUID | None,
        now: datetime | None = None,
    ) -> CreatedAgentRun:
        if thread_id is None:
            thread = AgentThread(
                user_id=user_id,
                title=question[:50],
            )
            self._session.add(thread)
            await self._session.flush()
            next_seq = 1
        else:
            thread = (
                await self._session.execute(
                    select(AgentThread)
                    .where(
                        AgentThread.id == thread_id,
                        AgentThread.user_id == user_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if thread is None:
                raise ThreadNotFoundError()
            if await self._has_active_run(thread.id):
                raise ActiveRunConflictError()
            next_seq = await self._next_message_seq(thread.id)

        quota_reservation = await reserve_daily_quota(
            self._session,
            user_id=user_id,
        )
        created_at = await database_now(self._session, now)
        deadline_at = deadline_for_run(created_at)
        thread.updated_at = created_at

        user_message = AgentMessage(
            thread_id=thread.id,
            seq=next_seq,
            role="user",
            content=question,
            missing_aspects=[],
        )
        self._session.add(user_message)
        await self._session.flush()

        run = AgentRun(
            thread_id=thread.id,
            user_message_id=user_message.id,
            status=AgentRunStatus.QUEUED.value,
            created_at=created_at,
            deadline_at=deadline_at,
            quota_usage_date=quota_reservation.usage_date,
        )
        self._session.add(run)
        await self._session.flush()
        return CreatedAgentRun(
            thread_id=thread.id,
            run_id=run.id,
            usage_date=quota_reservation.usage_date,
            used_count=quota_reservation.used_count,
        )

    async def mark_failed(
        self,
        run_id: uuid_mod.UUID,
        *,
        expected_attempt_epoch: int,
        error_code: AgentRunErrorCode,
    ) -> bool:
        result = await self._session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.status.in_(_ACTIVE_STATUSES),
                AgentRun.attempt_epoch == expected_attempt_epoch,
            )
            .values(
                status=AgentRunStatus.FAILED.value,
                error_code=error_code.value,
            )
            .execution_options(synchronize_session=False)
        )
        return (result.rowcount or 0) == 1

    async def mark_enqueue_failed(
        self,
        run_id: uuid_mod.UUID,
    ) -> bool:
        result = await self._session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.status == AgentRunStatus.QUEUED.value,
            )
            .values(
                status=AgentRunStatus.FAILED.value,
                error_code=AgentRunErrorCode.ENQUEUE_FAILED.value,
            )
            .execution_options(synchronize_session=False)
        )
        return (result.rowcount or 0) == 1

    async def mark_policy_blocked(
        self,
        run_id: uuid_mod.UUID,
        *,
        expected_attempt_epoch: int,
    ) -> bool:
        result = await self._session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.status == AgentRunStatus.RUNNING.value,
                AgentRun.attempt_epoch == expected_attempt_epoch,
            )
            .values(
                status=AgentRunStatus.POLICY_BLOCKED.value,
                assistant_message_id=None,
                error_code=None,
            )
            .execution_options(synchronize_session=False)
        )
        return (result.rowcount or 0) == 1

    async def start_run(
        self,
        run_id: uuid_mod.UUID,
        *,
        now: datetime | None = None,
    ) -> int | StartRunFailure:
        row = (
            await self._session.execute(
                select(
                    AgentRun,
                    AgentThread.user_id,
                )
                .join(AgentThread, AgentRun.thread_id == AgentThread.id)
                .where(AgentRun.id == run_id)
                .with_for_update(of=AgentRun)
                .execution_options(populate_existing=True)
            )
        ).one_or_none()
        if row is None:
            return StartRunFailure(StartRunFailureReason.RUN_NOT_FOUND)
        run, user_id = row
        if run.status in _TERMINAL_STATUSES:
            return StartRunFailure(StartRunFailureReason.ALREADY_FINISHED)

        if run.status not in _ACTIVE_STATUSES:
            logger.error(
                "agent_run_start_unexpected",
                run_id=str(run_id),
                observed_status=run.status,
            )
            return StartRunFailure(StartRunFailureReason.UNEXPECTED)

        now = await database_now(self._session, now)
        if now >= run.deadline_at:
            expired = await expire_run(
                self._session,
                run_id=run_id,
                expected_status=AgentRunStatus(run.status),
                expected_attempt_epoch=run.attempt_epoch,
                now=now,
            )
            if not expired:
                logger.error(
                    "agent_run_start_unexpected",
                    run_id=str(run_id),
                    cause="deadline_write",
                    observed_status=run.status,
                )
                return StartRunFailure(StartRunFailureReason.UNEXPECTED)
            if run.status == AgentRunStatus.QUEUED.value:
                outcome = await release_daily_quota(
                    self._session,
                    user_id=user_id,
                    usage_date=run.quota_usage_date,
                )
                daily_quota_observability.observe_release(
                    run_id=run_id,
                    outcome=outcome,
                )
            return StartRunFailure(StartRunFailureReason.DEADLINE_EXCEEDED)

        result = await self._session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.status.in_(_ACTIVE_STATUSES),
                AgentRun.deadline_at > now,
            )
            .values(
                status=AgentRunStatus.RUNNING.value,
                attempt_epoch=AgentRun.attempt_epoch + 1,
            )
            .returning(AgentRun.attempt_epoch)
            .execution_options(synchronize_session=False)
        )
        attempt_epoch = result.scalar_one_or_none()
        if attempt_epoch is None:
            logger.error(
                "agent_run_start_unexpected",
                run_id=str(run_id),
                cause="start_write",
                observed_status=run.status,
            )
            return StartRunFailure(StartRunFailureReason.UNEXPECTED)
        return attempt_epoch

    async def read_user_question_for_run(
        self,
        run_id: uuid_mod.UUID,
    ) -> tuple[uuid_mod.UUID, uuid_mod.UUID, UserQuestionMessage] | None:
        row = (
            await self._session.execute(
                select(
                    AgentThread.user_id,
                    AgentRun.thread_id,
                    AgentMessage.content,
                    AgentMessage.seq,
                )
                .join(AgentMessage, AgentRun.user_message_id == AgentMessage.id)
                .join(AgentThread, AgentRun.thread_id == AgentThread.id)
                .where(AgentRun.id == run_id)
            )
        ).one_or_none()
        if row is None:
            return None
        user_id, thread_id, content, seq = row
        return user_id, thread_id, UserQuestionMessage(content=content, seq=seq)

    async def decide_execution_continuation(
        self,
        *,
        run_id: uuid_mod.UUID,
        attempt_epoch: int,
        now: datetime | None = None,
    ) -> Continue | Stop:
        now = await database_now(self._session, now)
        deadline_at = (
            await self._session.execute(
                select(AgentRun.deadline_at).where(
                    AgentRun.id == run_id,
                    AgentRun.status == AgentRunStatus.RUNNING.value,
                    AgentRun.attempt_epoch == attempt_epoch,
                )
            )
        ).scalar_one_or_none()
        if deadline_at is None:
            return Stop(StopReason.NOT_CURRENT)
        if now < deadline_at:
            return Continue()

        expired = await expire_run(
            self._session,
            run_id=run_id,
            expected_status=AgentRunStatus.RUNNING,
            expected_attempt_epoch=attempt_epoch,
            now=now,
        )
        if not expired:
            return Stop(StopReason.NOT_CURRENT)
        return Stop(StopReason.DEADLINE_EXCEEDED)

    async def complete_run(
        self,
        *,
        run_id: uuid_mod.UUID,
        result: AnswerQuestionResult,
        expected_attempt_epoch: int,
        research_handoff: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> CompleteRunOutcome:
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
            return CompleteRunOutcome.TRANSITION_LOST
        run, thread = row
        if (
            run.status != AgentRunStatus.RUNNING.value
            or run.attempt_epoch != expected_attempt_epoch
        ):
            return CompleteRunOutcome.TRANSITION_LOST

        now = await database_now(self._session, now)
        if now >= run.deadline_at:
            expired = await expire_run(
                self._session,
                run_id=run_id,
                expected_status=AgentRunStatus.RUNNING,
                expected_attempt_epoch=expected_attempt_epoch,
                now=now,
            )
            if not expired:
                return CompleteRunOutcome.TRANSITION_LOST
            return CompleteRunOutcome.DEADLINE_EXCEEDED

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
                AgentRun.deadline_at > now,
            )
            .values(
                status=AgentRunStatus.COMPLETED.value,
                assistant_message_id=assistant_message.id,
            )
            .execution_options(synchronize_session=False)
        )
        if (update_result.rowcount or 0) != 1:
            raise RunTransitionLostError()
        thread.updated_at = now
        # Noneは「記録を追加しなかったRun」であり、既存handoffを消さない。
        if research_handoff is not None:
            thread.research_handoff = research_handoff
        return CompleteRunOutcome.COMPLETED

    async def read_run_for_user(
        self,
        *,
        run_id: uuid_mod.UUID,
        user_id: uuid_mod.UUID,
    ) -> ResearchRunResponse | None:
        run = (
            await self._session.execute(
                select(AgentRun)
                .join(AgentThread, AgentRun.thread_id == AgentThread.id)
                .where(
                    AgentRun.id == run_id,
                    AgentThread.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if run is None:
            return None
        return build_research_run_response(run=run)

    async def read_live_context_for_user(
        self,
        *,
        run_id: uuid_mod.UUID,
        user_id: uuid_mod.UUID,
    ) -> OwnedAgentRunLiveContext | None:
        row = (
            await self._session.execute(
                select(
                    AgentRun.id,
                    AgentRun.status,
                    AgentRun.attempt_epoch,
                    AgentRun.error_code,
                )
                .join(AgentThread, AgentRun.thread_id == AgentThread.id)
                .where(
                    AgentRun.id == run_id,
                    AgentThread.user_id == user_id,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        context_run_id, status_value, attempt_epoch, error_code = row
        return OwnedAgentRunLiveContext(
            run_id=context_run_id,
            status=AgentRunStatus(status_value),
            attempt_epoch=attempt_epoch,
            error_code=(AgentRunErrorCode(error_code) if error_code else None),
        )

    async def cancel_run_for_user(
        self,
        *,
        run_id: uuid_mod.UUID,
        user_id: uuid_mod.UUID,
    ) -> CancelRunCommandOutcome | None:
        owned_thread_ids = select(AgentThread.id).where(AgentThread.user_id == user_id)
        queued_result = await self._session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.status == AgentRunStatus.QUEUED.value,
                AgentRun.thread_id.in_(owned_thread_ids),
            )
            .values(
                status=AgentRunStatus.FAILED.value,
                error_code=AgentRunErrorCode.CANCELLED.value,
            )
            .returning(AgentRun.quota_usage_date)
            .execution_options(synchronize_session=False)
        )
        queued_row = queued_result.one_or_none()
        if queued_row is not None:
            quota_release_outcome = await release_daily_quota(
                self._session,
                user_id=user_id,
                usage_date=queued_row.quota_usage_date,
            )
            return CancelRunCommandOutcome(
                cancel_outcome=CancelRunOutcome.CANCELLED,
                quota_release_outcome=quota_release_outcome,
            )

        running_result = await self._session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.status == AgentRunStatus.RUNNING.value,
                AgentRun.thread_id.in_(owned_thread_ids),
            )
            .values(
                status=AgentRunStatus.FAILED.value,
                error_code=AgentRunErrorCode.CANCELLED.value,
            )
            .returning(AgentRun.attempt_epoch)
            .execution_options(synchronize_session=False)
        )
        running_attempt_epoch = running_result.scalar_one_or_none()
        if running_attempt_epoch is not None:
            return CancelRunCommandOutcome(
                cancel_outcome=CancelRunOutcome.CANCELLED,
                was_running=True,
                running_attempt_epoch=running_attempt_epoch,
                quota_release_outcome=DailyQuotaReleaseOutcome.NOT_ELIGIBLE,
            )

        terminal_status = (
            await self._session.execute(
                select(AgentRun.status).where(
                    AgentRun.id == run_id,
                    AgentRun.thread_id.in_(owned_thread_ids),
                )
            )
        ).scalar_one_or_none()
        if terminal_status is None:
            return None
        if AgentRunStatus(terminal_status) is AgentRunStatus.COMPLETED:
            return CancelRunCommandOutcome(CancelRunOutcome.ALREADY_COMPLETED)
        if AgentRunStatus(terminal_status) is AgentRunStatus.FAILED:
            return CancelRunCommandOutcome(CancelRunOutcome.ALREADY_FAILED)
        if AgentRunStatus(terminal_status) is AgentRunStatus.POLICY_BLOCKED:
            return CancelRunCommandOutcome(CancelRunOutcome.ALREADY_POLICY_BLOCKED)
        if AgentRunStatus(terminal_status) is AgentRunStatus.DEADLINE_EXCEEDED:
            return CancelRunCommandOutcome(CancelRunOutcome.ALREADY_DEADLINE_EXCEEDED)
        return None

    async def _has_active_run(self, thread_id: uuid_mod.UUID) -> bool:
        return (
            await self._session.execute(
                select(AgentRun.id)
                .where(
                    AgentRun.thread_id == thread_id,
                    AgentRun.status.in_(_ACTIVE_STATUSES),
                )
                .limit(1)
            )
        ).scalar_one_or_none() is not None

    async def _next_message_seq(self, thread_id: uuid_mod.UUID) -> int:
        value = (
            await self._session.execute(
                select(func.coalesce(func.max(AgentMessage.seq), 0) + 1).where(
                    AgentMessage.thread_id == thread_id
                )
            )
        ).scalar_one()
        return int(value)
