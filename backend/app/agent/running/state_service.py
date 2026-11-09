"""Runの状態を変える操作を、トランザクション・commit後の通知・DBの一時的な失敗のやり直しとともに行う。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.contract import AnswerQuestionResult
from app.agent.running.daily_quota import observability as daily_quota_observability
from app.agent.running.daily_quota.observability import DailyQuotaReleaseResult
from app.agent.running.deadline.deadline_exceeded import (
    DeadlineRunSweepResult,
    check_agent_run_deadline,
    sweep_deadline_exceeded_runs,
    sweep_deadline_exceeded_runs_for_thread,
)
from app.agent.running.finish_notice import (
    ClosedRun,
    ClosedRunningRun,
    LivePublisherFor,
    notify_closed_run,
)
from app.agent.running.state import (
    AnswerGenerationStarted,
    AttemptAlreadyStarted,
    AttemptStarted,
    CancelledByUser,
    CanContinue,
    CannotContinue,
    DeadlineExceeded,
    FailureRecorded,
    RunAlreadyCompleted,
    RunAlreadyFailed,
    RunCompleted,
    RunNotFound,
)
from app.agent.running.state_repository import AgentRunStateRepository
from app.agent.running.types import AgentRunErrorCode, AgentRunStatus
from app.db.retry_prospect import is_resolvable_by_retry
from app.db.translate import translate_database_error

__all__ = ["AgentRunStateService"]

# 一時的なDBの失敗だけを、受理期限と回答の時間枠を食わない範囲でやり直す。
_MAX_TRANSACTION_ATTEMPTS = 3
_RETRY_DELAY_SECONDS = 0.2


class AgentRunStateService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        publisher_for: LivePublisherFor,
    ) -> None:
        self._session_factory = session_factory
        self._publisher_for = publisher_for

    async def start_attempt(self, run_id: UUID) -> AttemptStarted | CannotContinue:
        return await self._operate(lambda repository: repository.start_attempt(run_id))

    async def check_continuation(
        self, run_id: UUID, *, attempt_epoch: int
    ) -> CanContinue | CannotContinue:
        return await self._operate(
            lambda repository: repository.check_continuation(
                run_id, attempt_epoch=attempt_epoch
            )
        )

    async def start_answer_generation(
        self, run_id: UUID, *, attempt_epoch: int
    ) -> AnswerGenerationStarted | CannotContinue:
        return await self._operate(
            lambda repository: repository.start_answer_generation(
                run_id, attempt_epoch=attempt_epoch
            )
        )

    async def authorize_answer_regeneration(
        self, run_id: UUID, *, attempt_epoch: int
    ) -> CanContinue | CannotContinue:
        return await self._operate(
            lambda repository: repository.authorize_answer_regeneration(
                run_id, attempt_epoch=attempt_epoch
            )
        )

    async def complete(
        self,
        run_id: UUID,
        *,
        attempt_epoch: int,
        result: AnswerQuestionResult,
        research_handoff: dict[str, Any] | None,
    ) -> RunCompleted | CannotContinue:
        return await self._operate(
            lambda repository: repository.complete(
                run_id,
                attempt_epoch=attempt_epoch,
                result=result,
                research_handoff=research_handoff,
            )
        )

    async def record_failure(
        self,
        run_id: UUID,
        *,
        attempt_epoch: int,
        error_code: AgentRunErrorCode,
    ) -> FailureRecorded | CannotContinue:
        return await self._operate(
            lambda repository: repository.record_failure(
                run_id, attempt_epoch=attempt_epoch, error_code=error_code
            )
        )

    async def cancel_for_user(
        self, run_id: UUID, *, user_id: UUID
    ) -> (
        CancelledByUser
        | DeadlineExceeded
        | RunAlreadyCompleted
        | RunAlreadyFailed
        | RunNotFound
    ):
        return await self._operate(
            lambda repository: repository.cancel_for_user(run_id, user_id=user_id)
        )

    async def record_enqueue_failure(
        self, run_id: UUID
    ) -> (
        FailureRecorded
        | AttemptAlreadyStarted
        | CancelledByUser
        | DeadlineExceeded
        | RunAlreadyCompleted
        | RunAlreadyFailed
        | RunNotFound
    ):
        return await self._operate(
            lambda repository: repository.record_enqueue_failure(run_id)
        )

    async def sweep_deadline_exceeded_runs(self) -> DeadlineRunSweepResult:
        result = await self._in_transaction(sweep_deadline_exceeded_runs)
        await self._notify_swept(result)
        return result

    async def sweep_deadline_exceeded_runs_for_thread(
        self, *, thread_id: UUID, user_id: UUID
    ) -> DeadlineRunSweepResult:
        result = await self._in_transaction(
            lambda session: sweep_deadline_exceeded_runs_for_thread(
                session, thread_id=thread_id, user_id=user_id
            )
        )
        await self._notify_swept(result)
        return result

    async def check_run_deadline(self, run_id: UUID) -> DeadlineRunSweepResult:
        result = await self._in_transaction(
            lambda session: check_agent_run_deadline(session, run_id=run_id)
        )
        await self._notify_swept(result)
        return result

    async def _operate[Result](
        self,
        operation: Callable[[AgentRunStateRepository], Awaitable[Result]],
    ) -> Result:
        async def work(session: AsyncSession) -> tuple[Result, tuple[ClosedRun, ...]]:
            repository = AgentRunStateRepository(session)
            return await operation(repository), tuple(repository.closed_runs)

        result, closed_runs = await self._in_transaction(work)
        for closed in closed_runs:
            await notify_closed_run(closed, publisher_for=self._publisher_for)
        return result

    async def _notify_swept(self, result: DeadlineRunSweepResult) -> None:
        released: tuple[tuple[DailyQuotaReleaseResult, int], ...] = (
            ("released", result.queued_quota_released_count),
            ("not_eligible", result.queued_quota_not_eligible_count),
            ("inconsistent", result.queued_quota_inconsistent_count),
        )
        for quota_result, count in released:
            if count > 0:
                with suppress(Exception):
                    daily_quota_observability.record_daily_quota_release(
                        result=quota_result,
                        count=count,
                    )
        with suppress(Exception):
            daily_quota_observability.observe_stale_reservations(
                queued_count=result.queued_quota_inconsistent_count,
                running_count=result.running_quota_reservation_count,
            )
        for running_run in result.running_terminal_runs:
            await notify_closed_run(
                ClosedRunningRun(
                    run_id=running_run.run_id,
                    attempt_epoch=running_run.attempt_epoch,
                    status=AgentRunStatus.DEADLINE_EXCEEDED,
                    error_code=None,
                ),
                publisher_for=self._publisher_for,
            )

    async def _in_transaction[Result](
        self, work: Callable[[AsyncSession], Awaitable[Result]]
    ) -> Result:
        attempt_number = 1
        while True:
            async with self._session_factory() as session:
                await session.begin()
                try:
                    result = await work(session)
                except SQLAlchemyError as exc:
                    error = translate_database_error(exc)
                    if attempt_number >= _MAX_TRANSACTION_ATTEMPTS or not (
                        is_resolvable_by_retry(error)
                    ):
                        raise error from exc
                else:
                    try:
                        await session.commit()
                    except SQLAlchemyError as exc:
                        # commit中の失敗は書けたか分からないので、やり直さない。
                        raise translate_database_error(exc) from exc
                    return result
            await asyncio.sleep(_RETRY_DELAY_SECONDS * attempt_number)
            attempt_number += 1
