"""Runの状態を変える操作を、1回のトランザクションで確認・書き込みし、commit後に閉じたRunを通知する。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
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
    AttemptAlreadyStarted,
    AttemptStarted,
    CancelledByUser,
    CanContinue,
    CannotContinue,
    DeadlineExceeded,
    FailureRecorded,
    RunAlreadyEnded,
    RunCompleted,
    RunNotFound,
    RunPhase,
)
from app.agent.running.state_repository import AgentRunStateRepository
from app.agent.running.types import AgentRunErrorCode, AgentRunStatus, RunAttempt
from app.agent.threads.citation_integrity import warn_on_citation_source_mismatch
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
        async with self._run_operation(run_id) as operation:
            decision = await operation.repository.check_start(run_id)
            await operation.close_if_expired(decision)
            match decision:
                case CanContinue():
                    current_run = operation.repository.current_run
                    attempt = RunAttempt(
                        run_id=run_id, epoch=current_run.state.attempt_epoch + 1
                    )
                    await operation.repository.mark_attempt_started(
                        attempt_epoch=attempt.epoch
                    )
                    return AttemptStarted(attempt=attempt)
                case CannotContinue() as stopped:
                    return stopped

    @_retry_transient_database_failure
    async def authorize_stage_entry(
        self, attempt: RunAttempt
    ) -> CanContinue | CannotContinue:
        async with self._run_operation(attempt.run_id) as operation:
            decision = await operation.repository.check_attempt(
                attempt, phases={RunPhase.PREPARING}
            )
            await operation.close_if_expired(decision)
            return decision

    @_retry_transient_database_failure
    async def start_answer_generation(
        self, attempt: RunAttempt
    ) -> AnswerGenerationStarted | CannotContinue:
        async with self._run_operation(attempt.run_id) as operation:
            decision = await operation.repository.check_attempt(
                attempt, phases={RunPhase.PREPARING}
            )
            await operation.close_if_expired(decision)
            match decision:
                case CanContinue():
                    answer_started_at = operation.repository.current_run.now
                    await operation.repository.mark_answer_started(
                        answer_started_at=answer_started_at
                    )
                    return AnswerGenerationStarted(
                        run_id=attempt.run_id, answer_started_at=answer_started_at
                    )
                case CannotContinue() as stopped:
                    return stopped

    @_retry_transient_database_failure
    async def authorize_answer_regeneration(
        self, attempt: RunAttempt
    ) -> CanContinue | CannotContinue:
        async with self._run_operation(attempt.run_id) as operation:
            decision = await operation.repository.check_attempt(
                attempt, phases={RunPhase.ANSWERING}
            )
            await operation.close_if_expired(decision)
            return decision

    @_retry_transient_database_failure
    async def complete(
        self,
        attempt: RunAttempt,
        *,
        result: AnswerQuestionResult,
        research_handoff: dict[str, Any] | None,
    ) -> RunCompleted | CannotContinue:
        async with self._run_operation(attempt.run_id) as operation:
            repository = operation.repository
            decision = await repository.check_attempt(
                attempt, phases={RunPhase.ANSWERING}, lock_thread=True
            )
            await operation.close_if_expired(decision)
            match decision:
                case CanContinue():
                    pass
                case CannotContinue() as stopped:
                    return stopped

            warn_on_citation_source_mismatch(
                run_id=attempt.run_id,
                answer=result.answer,
                source_refs=[source.source_ref for source in result.sources],
            )
            assistant_message_id = await repository.add_assistant_message(result=result)
            await operation.close(
                status=AgentRunStatus.COMPLETED,
                assistant_message_id=assistant_message_id,
            )
            repository.touch_thread(updated_at=repository.current_run.now)
            # Noneは「記録を追加しなかったRun」であり、既存handoffを消さない。
            if research_handoff is not None:
                repository.replace_research_handoff(research_handoff)
            return RunCompleted()

    @_retry_transient_database_failure
    async def record_failure(
        self, attempt: RunAttempt, *, error_code: AgentRunErrorCode
    ) -> FailureRecorded | CannotContinue:
        async with self._run_operation(attempt.run_id) as operation:
            decision = await operation.repository.check_attempt(
                attempt, phases={RunPhase.PREPARING, RunPhase.ANSWERING}
            )
            await operation.close_if_expired(decision)
            match decision:
                case CanContinue():
                    await operation.close(
                        status=AgentRunStatus.FAILED, error_code=error_code
                    )
                    return FailureRecorded()
                case CannotContinue() as stopped:
                    return stopped

    @_retry_transient_database_failure
    async def cancel_for_user(
        self, run_id: UUID, *, user_id: UUID
    ) -> CancelledByUser | DeadlineExceeded | RunAlreadyEnded | RunNotFound:
        async with self._run_operation(run_id) as operation:
            decision = await operation.repository.check_cancel(run_id, owner_id=user_id)
            await operation.close_if_expired(decision)
            match decision:
                case CanContinue():
                    await operation.close(
                        status=AgentRunStatus.FAILED,
                        error_code=AgentRunErrorCode.CANCELLED,
                    )
                    return CancelledByUser()
                case CannotContinue() as stopped:
                    return stopped

    @_retry_transient_database_failure
    async def record_enqueue_failure(
        self, run_id: UUID
    ) -> (
        FailureRecorded
        | AttemptAlreadyStarted
        | DeadlineExceeded
        | RunAlreadyEnded
        | RunNotFound
    ):
        async with self._run_operation(run_id) as operation:
            decision = await operation.repository.check_enqueue_failure(run_id)
            await operation.close_if_expired(decision)
            match decision:
                case CanContinue():
                    await operation.close(
                        status=AgentRunStatus.FAILED,
                        error_code=AgentRunErrorCode.ENQUEUE_FAILED,
                    )
                    return FailureRecorded()
                case (AttemptAlreadyStarted() | CannotContinue()) as skipped:
                    return skipped

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

    @asynccontextmanager
    async def _run_operation(self, run_id: UUID) -> AsyncIterator[_RunOperation]:
        async with self._transaction() as session:
            operation = _RunOperation(
                run_id=run_id,
                repository=AgentRunStateRepository(session, now=self._now),
            )
            yield operation
        # 中身は一時的なDBの失敗でやり直されるので、閉じたRunの通知はcommitの後に行う。
        if operation.closed_run is not None:
            await notify_closed_run(
                operation.closed_run, publisher_for=self._publisher_for
            )

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


@dataclass(slots=True)
class _RunOperation:
    """1回の操作の間だけ使う、確認と書き込みの口と、commit後に通知する閉じたRun。"""

    run_id: UUID
    repository: AgentRunStateRepository
    closed_run: ClosedRun | None = None

    async def close_if_expired(self, decision: object) -> None:
        """期限切れの判定は、まだ終わっていないRunにだけ返る。"""
        match decision:
            case DeadlineExceeded():
                await self.close(status=AgentRunStatus.DEADLINE_EXCEEDED)

    async def close(
        self,
        *,
        status: AgentRunStatus,
        error_code: AgentRunErrorCode | None = None,
        assistant_message_id: UUID | None = None,
    ) -> None:
        current_run = self.repository.current_run
        was_queued = current_run.state.phase is RunPhase.QUEUED
        await self.repository.close(
            status=status,
            error_code=error_code,
            assistant_message_id=assistant_message_id,
        )
        # 待機中のまま閉じたRunは、受付で予約した利用枠を返す。
        if was_queued:
            self.closed_run = ClosedQueuedRun(
                run_id=self.run_id,
                quota_release_outcome=await self.repository.release_daily_quota(),
            )
            return
        self.closed_run = ClosedRunningRun(
            attempt=RunAttempt(
                run_id=self.run_id, epoch=current_run.state.attempt_epoch
            ),
            status=status,
            error_code=error_code,
        )
