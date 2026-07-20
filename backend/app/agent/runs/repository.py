"""Repository for agent run lifecycle commands and state."""

from __future__ import annotations

import uuid as uuid_mod
from datetime import UTC, date, datetime, timedelta

import structlog
from sqlalchemy import (
    Date,
    Integer,
    Select,
    bindparam,
    cast,
    func,
    literal,
    select,
    true,
    update,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.agent.contract import AnswerQuestionResult
from app.agent.runs.citation_integrity import assess_citation_integrity
from app.agent.runs.contracts import (
    ActiveRunConflictError,
    CancelRunOutcome,
    CancelRunResult,
    CreatedAgentRun,
    DailyRequestLimitExceededError,
    OwnedAgentRunLiveContext,
    PreparedAgentRun,
    RunTransitionLostError,
    ThreadNotFoundError,
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
from app.models.agent_user_daily_quota import AgentUserDailyQuota
from app.schemas.research import ResearchRunResponse

_ACTIVE_STATUSES = (AgentRunStatus.QUEUED.value, AgentRunStatus.RUNNING.value)
_TERMINAL_STATUSES = (AgentRunStatus.COMPLETED.value, AgentRunStatus.FAILED.value)
_STALE_AFTER = timedelta(minutes=20)
_DAILY_REQUEST_LIMIT = 10
logger = structlog.get_logger(__name__)


def _build_daily_quota_reservation_statement(
    *,
    user_id: uuid_mod.UUID,
    clock_expression: ColumnElement[datetime] | None = None,
) -> Select[tuple[datetime, date, datetime, int | None]]:
    observed_at_expression = (
        clock_expression if clock_expression is not None else func.statement_timestamp()
    )
    quota_clock = (
        select(observed_at_expression.label("observed_at"))
        .cte("quota_clock")
        .prefix_with("MATERIALIZED", dialect="postgresql")
    )
    usage_date_expression = cast(
        quota_clock.c.observed_at.op("AT TIME ZONE")(literal("Asia/Tokyo")),
        Date(),
    ).label("usage_date")
    quota_day = (
        select(quota_clock.c.observed_at, usage_date_expression)
        .select_from(quota_clock)
        .cte("quota_day")
        .prefix_with("MATERIALIZED", dialect="postgresql")
    )
    reservation = (
        pg_insert(AgentUserDailyQuota)
        .from_select(
            ["user_id", "usage_date", "used_count"],
            select(
                bindparam("user_id", user_id, type_=PgUUID(as_uuid=True)),
                quota_day.c.usage_date,
                literal(1, type_=Integer()),
            ).select_from(quota_day),
        )
        .on_conflict_do_update(
            index_elements=[
                AgentUserDailyQuota.user_id,
                AgentUserDailyQuota.usage_date,
            ],
            set_={
                "used_count": AgentUserDailyQuota.used_count + 1,
            },
            where=(
                AgentUserDailyQuota.used_count
                < bindparam(
                    "daily_limit",
                    _DAILY_REQUEST_LIMIT,
                    type_=Integer(),
                )
            ),
        )
        .returning(AgentUserDailyQuota.used_count)
        .cte("reservation")
    )
    return select(
        quota_day.c.observed_at,
        quota_day.c.usage_date,
        func.clock_timestamp().label("decided_at"),
        reservation.c.used_count,
    ).select_from(quota_day.outerjoin(reservation, true()))


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

        quota_reservation = (
            (
                await self._session.execute(
                    _build_daily_quota_reservation_statement(user_id=user_id)
                )
            )
            .mappings()
            .one()
        )
        usage_date = quota_reservation["usage_date"]
        used_count = quota_reservation["used_count"]
        if used_count is None:
            raise DailyRequestLimitExceededError(
                usage_date=usage_date,
                observed_at=quota_reservation["observed_at"],
                decided_at=quota_reservation["decided_at"],
                limit=_DAILY_REQUEST_LIMIT,
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
            quota_usage_date=usage_date,
        )
        self._session.add(run)
        await self._session.flush()
        return CreatedAgentRun(
            thread_id=thread.id,
            run_id=run.id,
            usage_date=usage_date,
            used_count=used_count,
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
    ) -> CancelRunResult | None:
        now = now or datetime.now(UTC)
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
        status_value = AgentRunStatus(run.status)
        if status_value is AgentRunStatus.COMPLETED:
            return CancelRunResult(CancelRunOutcome.ALREADY_COMPLETED)
        if status_value is AgentRunStatus.FAILED:
            return CancelRunResult(CancelRunOutcome.ALREADY_FAILED)

        result = await self._session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.status.in_(_ACTIVE_STATUSES),
                AgentRun.thread_id.in_(
                    select(AgentThread.id).where(AgentThread.user_id == user_id)
                ),
            )
            .values(
                status=AgentRunStatus.FAILED.value,
                error_code=AgentRunErrorCode.CANCELLED.value,
                completed_at=now,
            )
            .returning(AgentRun.attempt_epoch)
            .execution_options(synchronize_session=False)
        )
        attempt_epoch = result.scalar_one_or_none()
        if attempt_epoch is not None:
            return CancelRunResult(
                outcome=CancelRunOutcome.CANCELLED,
                attempt_epoch=attempt_epoch,
            )

        refreshed_status = (
            await self._session.execute(
                select(AgentRun.status)
                .join(AgentThread, AgentRun.thread_id == AgentThread.id)
                .where(
                    AgentRun.id == run_id,
                    AgentThread.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if refreshed_status is None:
            return None
        if AgentRunStatus(refreshed_status) is AgentRunStatus.COMPLETED:
            return CancelRunResult(CancelRunOutcome.ALREADY_COMPLETED)
        return CancelRunResult(CancelRunOutcome.ALREADY_FAILED)

    async def sweep_stale_runs(
        self,
        *,
        now: datetime | None = None,
    ) -> int:
        now = now or datetime.now(UTC)
        cutoff = now - _STALE_AFTER
        result = await self._session.execute(
            update(AgentRun)
            .where(
                AgentRun.status.in_(_ACTIVE_STATUSES),
                func.coalesce(AgentRun.started_at, AgentRun.created_at) < cutoff,
            )
            .values(
                status=AgentRunStatus.FAILED.value,
                error_code=AgentRunErrorCode.STALE.value,
                completed_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        return result.rowcount or 0

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
