"""Repository for agent run lifecycle commands and state."""

from __future__ import annotations

import uuid as uuid_mod
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.contract import AnswerQuestionResult
from app.agent.runs.citation_integrity import assess_citation_integrity
from app.agent.runs.contracts import (
    ActiveRunConflictError,
    CancelRunCommandOutcome,
    CancelRunOutcome,
    CreatedAgentRun,
    OwnedAgentRunLiveContext,
    PreparedAgentRun,
    RunTransitionLostError,
    StaleRunSweepResult,
    ThreadNotFoundError,
)
from app.agent.runs.daily_quota.contracts import DailyQuotaReleaseOutcome
from app.agent.runs.daily_quota.persistence import (
    release_daily_quota,
    reserve_daily_quota,
)
from app.agent.runs.projection import build_research_run_response
from app.agent.runs.result_mapper import (
    build_assistant_message_for_result,
    build_source_rows_for_message,
)
from app.agent.runs.types import AgentRunErrorCode, AgentRunStatus
from app.models.agent_message import AgentMessage
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread
from app.schemas.research import ResearchRunResponse

_ACTIVE_STATUSES = (AgentRunStatus.QUEUED.value, AgentRunStatus.RUNNING.value)
_TERMINAL_STATUSES = (
    AgentRunStatus.COMPLETED.value,
    AgentRunStatus.POLICY_BLOCKED.value,
    AgentRunStatus.FAILED.value,
)
_STALE_AFTER = timedelta(minutes=20)
logger = structlog.get_logger(__name__)


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
        now = now or datetime.now(UTC)
        if thread_id is None:
            thread = AgentThread(
                user_id=user_id,
                title=question[:50],
                updated_at=now,
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
            thread.updated_at = now

        quota_reservation = await reserve_daily_quota(
            self._session,
            user_id=user_id,
        )

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
        now: datetime | None = None,
    ) -> bool:
        now = now or datetime.now(UTC)
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
                completed_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        return (result.rowcount or 0) == 1

    async def mark_enqueue_failed(
        self,
        run_id: uuid_mod.UUID,
        *,
        now: datetime | None = None,
    ) -> bool:
        now = now or datetime.now(UTC)
        result = await self._session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.status == AgentRunStatus.QUEUED.value,
            )
            .values(
                status=AgentRunStatus.FAILED.value,
                error_code=AgentRunErrorCode.ENQUEUE_FAILED.value,
                completed_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        return (result.rowcount or 0) == 1

    async def mark_policy_blocked(
        self,
        run_id: uuid_mod.UUID,
        *,
        expected_attempt_epoch: int,
        now: datetime | None = None,
    ) -> bool:
        now = now or datetime.now(UTC)
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
                progress_stage=None,
                completed_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        return (result.rowcount or 0) == 1

    async def acquire_for_execution(
        self,
        run_id: uuid_mod.UUID,
        *,
        now: datetime | None = None,
    ) -> PreparedAgentRun | None:
        now = now or datetime.now(UTC)
        row = (
            await self._session.execute(
                select(AgentRun, AgentMessage.content, AgentMessage.seq)
                .join(AgentMessage, AgentRun.user_message_id == AgentMessage.id)
                .where(AgentRun.id == run_id)
            )
        ).one_or_none()
        if row is None:
            return None
        run, question, user_message_seq = row
        if run.status in _TERMINAL_STATUSES:
            return None
        result = await self._session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.status.in_(_ACTIVE_STATUSES),
            )
            .values(
                status=AgentRunStatus.RUNNING.value,
                started_at=now,
                attempt_epoch=AgentRun.attempt_epoch + 1,
                progress_stage=None,
            )
            .returning(AgentRun.attempt_epoch)
            .execution_options(synchronize_session=False)
        )
        attempt_epoch = result.scalar_one_or_none()
        if attempt_epoch is None:
            return None
        return PreparedAgentRun(
            run_id=run.id,
            thread_id=run.thread_id,
            question=question,
            user_message_seq=user_message_seq,
            attempt_epoch=attempt_epoch,
        )

    async def is_execution_current(
        self,
        *,
        run_id: uuid_mod.UUID,
        attempt_epoch: int,
    ) -> bool:
        value = (
            await self._session.execute(
                select(1)
                .select_from(AgentRun)
                .where(
                    AgentRun.id == run_id,
                    AgentRun.status == AgentRunStatus.RUNNING.value,
                    AgentRun.attempt_epoch == attempt_epoch,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return value is not None

    async def complete_run(
        self,
        *,
        run_id: uuid_mod.UUID,
        result: AnswerQuestionResult,
        expected_attempt_epoch: int,
        now: datetime | None = None,
    ) -> bool:
        now = now or datetime.now(UTC)
        run = await self._session.get(AgentRun, run_id)
        if run is None or run.status in _TERMINAL_STATUSES:
            return False
        thread = (
            await self._session.execute(
                select(AgentThread)
                .where(AgentThread.id == run.thread_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if thread is None:
            return False

        assistant_message = build_assistant_message_for_result(
            thread_id=thread.id,
            seq=await self._next_message_seq(thread.id),
            result=result,
        )
        self._session.add(assistant_message)
        await self._session.flush()
        source_rows = build_source_rows_for_message(assistant_message, result)
        _warn_on_citation_source_mismatch(
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
            )
            .values(
                status=AgentRunStatus.COMPLETED.value,
                assistant_message_id=assistant_message.id,
                completed_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if (update_result.rowcount or 0) != 1:
            raise RunTransitionLostError()
        thread.updated_at = now
        return True

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
        now: datetime | None = None,
    ) -> CancelRunCommandOutcome | None:
        now = now or datetime.now(UTC)
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
                completed_at=now,
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
                completed_at=now,
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
        return None

    async def sweep_stale_runs(
        self,
        *,
        now: datetime | None = None,
    ) -> StaleRunSweepResult:
        now = now or datetime.now(UTC)
        cutoff = now - _STALE_AFTER
        candidate_rows = (
            (
                await self._session.execute(
                    select(
                        AgentRun.id,
                        AgentRun.status,
                        AgentRun.quota_usage_date,
                    )
                    .where(
                        AgentRun.status.in_(_ACTIVE_STATUSES),
                        func.coalesce(AgentRun.started_at, AgentRun.created_at)
                        < cutoff,
                    )
                    .order_by(AgentRun.id)
                    .with_for_update()
                )
            )
            .tuples()
            .all()
        )
        if not candidate_rows:
            return StaleRunSweepResult(
                total_count=0,
                quota_queued_count=0,
                quota_running_count=0,
            )

        candidate_ids = [run_id for run_id, _status, _quota_date in candidate_rows]
        updated_ids = set(
            (
                await self._session.execute(
                    update(AgentRun)
                    .where(
                        AgentRun.id.in_(candidate_ids),
                        AgentRun.status.in_(_ACTIVE_STATUSES),
                    )
                    .values(
                        status=AgentRunStatus.FAILED.value,
                        error_code=AgentRunErrorCode.STALE.value,
                        completed_at=now,
                    )
                    .returning(AgentRun.id)
                    .execution_options(synchronize_session=False)
                )
            )
            .scalars()
            .all()
        )
        if updated_ids != set(candidate_ids):
            raise RuntimeError("stale run sweep lost a locked candidate")

        return StaleRunSweepResult(
            total_count=len(updated_ids),
            quota_queued_count=sum(
                status == AgentRunStatus.QUEUED.value and quota_date is not None
                for _run_id, status, quota_date in candidate_rows
            ),
            quota_running_count=sum(
                status == AgentRunStatus.RUNNING.value and quota_date is not None
                for _run_id, status, quota_date in candidate_rows
            ),
        )

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


def _warn_on_citation_source_mismatch(
    *,
    run_id: uuid_mod.UUID,
    answer: str,
    source_refs: list[str],
) -> None:
    report = assess_citation_integrity(answer=answer, source_refs=source_refs)
    if not report.has_mismatch:
        return
    logger.warning(
        "agent_citation_source_mismatch",
        run_id=str(run_id),
        marker_without_source_refs=list(report.marker_without_source_refs),
        source_without_marker_refs=list(report.source_without_marker_refs),
    )
