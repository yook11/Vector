"""Runの状態を変える操作を、条件を満たすRunにだけ1回のトランザクションで書き、commit後に閉じたRunを通知する。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from functools import wraps
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
    ClosedQueuedRun,
    ClosedRun,
    ClosedRunningRun,
    LivePublisherFor,
    notify_closed_run,
)
from app.agent.running.state import (
    AnswerGenerationStarted,
    AttemptStarted,
    CancelledByUser,
    CanContinue,
    CannotContinue,
    FailureRecorded,
    RunCompleted,
    RunNotFound,
)
from app.agent.running.state_repository import AgentRunStateRepository
from app.agent.running.types import AgentRunErrorCode, AgentRunStatus, RunAttempt
from app.db.retry_prospect import is_resolvable_by_retry
from app.db.translate import translate_database_error

__all__ = ["AgentRunStateService"]

# 一時的なDBの失敗だけを、受理期限と回答の時間枠を食わない範囲でやり直す。
_MAX_TRANSACTION_ATTEMPTS = 3
_RETRY_DELAY_SECONDS = 0.2


def _retry_transient_database_failure[**P, R](
    operation: Callable[P, Coroutine[Any, Any, R]],
) -> Callable[P, Coroutine[Any, Any, R]]:
    """一時的なDBの失敗だけを、操作の最初からやり直す。"""

    @wraps(operation)
    async def retrying(*args: P.args, **kwargs: P.kwargs) -> R:
        for attempt_number in range(1, _MAX_TRANSACTION_ATTEMPTS + 1):
            try:
                return await operation(*args, **kwargs)
            except SQLAlchemyError as exc:
                error = translate_database_error(exc)
                is_last_attempt = attempt_number == _MAX_TRANSACTION_ATTEMPTS
                if is_last_attempt or not is_resolvable_by_retry(error):
                    raise error from exc
            # 操作を抜けてsessionを閉じ、接続を返してから待つ。
            await asyncio.sleep(_RETRY_DELAY_SECONDS * attempt_number)

    return retrying


class AgentRunStateService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        publisher_for: LivePublisherFor,
        now: datetime | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._publisher_for = publisher_for
        # テストでDBの時刻の代わりに使う。
        self._now = now

    @_retry_transient_database_failure
    async def start_attempt(self, run_id: UUID) -> AttemptStarted | CannotContinue:
        async with self._transaction() as session:
            attempt = await self._repository(session).start_attempt(run_id)
        if attempt is None:
            return CannotContinue()
        return AttemptStarted(attempt=attempt)

    @_retry_transient_database_failure
    async def authorize_continuation(
        self, attempt: RunAttempt
    ) -> CanContinue | CannotContinue:
        async with self._transaction() as session:
            can_continue = await self._repository(session).can_continue(attempt)
        if not can_continue:
            return CannotContinue()
        return CanContinue()

    @_retry_transient_database_failure
    async def start_answer_generation(
        self, attempt: RunAttempt
    ) -> AnswerGenerationStarted | CannotContinue:
        async with self._transaction() as session:
            answer_started_at = await self._repository(session).start_answer(attempt)
        if answer_started_at is None:
            return CannotContinue()
        return AnswerGenerationStarted(
            run_id=attempt.run_id, answer_started_at=answer_started_at
        )

    @_retry_transient_database_failure
    async def complete(
        self,
        attempt: RunAttempt,
        *,
        result: AnswerQuestionResult,
        research_handoff: dict[str, Any] | None,
    ) -> RunCompleted | CannotContinue:
        async with self._transaction() as session:
            completed = await self._repository(session).complete(
                attempt, result=result, research_handoff=research_handoff
            )
        if not completed:
            return CannotContinue()
        await notify_closed_run(
            ClosedRunningRun(
                attempt=attempt, status=AgentRunStatus.COMPLETED, error_code=None
            ),
            publisher_for=self._publisher_for,
        )
        return RunCompleted()

    @_retry_transient_database_failure
    async def record_failure(
        self, attempt: RunAttempt, *, error_code: AgentRunErrorCode
    ) -> FailureRecorded | CannotContinue:
        async with self._transaction() as session:
            failed = await self._repository(session).fail_attempt(
                attempt, error_code=error_code
            )
        if not failed:
            return CannotContinue()
        await notify_closed_run(
            ClosedRunningRun(
                attempt=attempt, status=AgentRunStatus.FAILED, error_code=error_code
            ),
            publisher_for=self._publisher_for,
        )
        return FailureRecorded()

    @_retry_transient_database_failure
    async def cancel_for_user(
        self, run_id: UUID, *, user_id: UUID
    ) -> CancelledByUser | CannotContinue | RunNotFound:
        async with self._transaction() as session:
            repository = self._repository(session)
            closed = await repository.cancel(run_id, owner_id=user_id)
            if closed is None:
                if not await repository.exists_for_owner(run_id, owner_id=user_id):
                    return RunNotFound()
                return CannotContinue()
            closed_run: ClosedRun
            if closed.was_queued:
                # 待機中のまま閉じたRunは、受付で予約した利用枠を返す。
                closed_run = ClosedQueuedRun(
                    run_id=run_id,
                    quota_release_outcome=await repository.release_daily_quota(
                        user_id=closed.user_id, usage_date=closed.quota_usage_date
                    ),
                )
            else:
                closed_run = ClosedRunningRun(
                    attempt=RunAttempt(run_id=run_id, epoch=closed.attempt_epoch),
                    status=AgentRunStatus.FAILED,
                    error_code=AgentRunErrorCode.CANCELLED,
                )
        await notify_closed_run(closed_run, publisher_for=self._publisher_for)
        return CancelledByUser()

    @_retry_transient_database_failure
    async def record_enqueue_failure(
        self, run_id: UUID
    ) -> FailureRecorded | CannotContinue:
        async with self._transaction() as session:
            repository = self._repository(session)
            closed = await repository.fail_queued(run_id)
            if closed is None:
                return CannotContinue()
            # 待機中のまま閉じたRunは、受付で予約した利用枠を返す。
            quota_release_outcome = await repository.release_daily_quota(
                user_id=closed.user_id, usage_date=closed.quota_usage_date
            )
        await notify_closed_run(
            ClosedQueuedRun(run_id=run_id, quota_release_outcome=quota_release_outcome),
            publisher_for=self._publisher_for,
        )
        return FailureRecorded()

    @_retry_transient_database_failure
    async def sweep_deadline_exceeded_runs(self) -> DeadlineRunSweepResult:
        async with self._transaction() as session:
            result = await sweep_deadline_exceeded_runs(session)
        await self._notify_swept(result)
        return result

    @_retry_transient_database_failure
    async def sweep_deadline_exceeded_runs_for_thread(
        self, *, thread_id: UUID, user_id: UUID
    ) -> DeadlineRunSweepResult:
        async with self._transaction() as session:
            result = await sweep_deadline_exceeded_runs_for_thread(
                session, thread_id=thread_id, user_id=user_id
            )
        await self._notify_swept(result)
        return result

    @_retry_transient_database_failure
    async def check_run_deadline(self, run_id: UUID) -> DeadlineRunSweepResult:
        async with self._transaction() as session:
            result = await check_agent_run_deadline(session, run_id=run_id)
        await self._notify_swept(result)
        return result

    def _repository(self, session: AsyncSession) -> AgentRunStateRepository:
        return AgentRunStateRepository(session, now=self._now)

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[AsyncSession]:
        async with self._session_factory() as session:
            await session.begin()
            yield session
            try:
                await session.commit()
            except SQLAlchemyError as exc:
                # commit中の失敗は書けたか分からないので、やり直さない。
                raise translate_database_error(exc) from exc

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
        for attempt in result.running_terminal_runs:
            await notify_closed_run(
                ClosedRunningRun(
                    attempt=attempt,
                    status=AgentRunStatus.DEADLINE_EXCEEDED,
                    error_code=None,
                ),
                publisher_for=self._publisher_for,
            )
