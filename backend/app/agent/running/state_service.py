"""Runの状態を変える操作を、1回のトランザクションでロック・判定・書き込みし、commit後に閉じたRunを通知する。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
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
    AttemptSuperseded,
    CancelledByUser,
    CanContinue,
    CannotContinue,
    DeadlineExceeded,
    FailureRecorded,
    RunAlreadyCompleted,
    RunAlreadyFailed,
    RunCompleted,
    RunNotFound,
    RunPhase,
)
from app.agent.running.state_repository import AgentRunStateRepository, CurrentRun
from app.agent.running.types import AgentRunErrorCode, AgentRunStatus
from app.agent.threads.citation_integrity import warn_on_citation_source_mismatch
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
        now: datetime | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._publisher_for = publisher_for
        # テストでDBの時刻の代わりに使う。
        self._now = now

    async def start_attempt(self, run_id: UUID) -> AttemptStarted | CannotContinue:
        async def on_locked_run(operation: _RunOperation, current_run: CurrentRun):
            decision = current_run.state.check_start(now=current_run.now)
            await operation.close_if_expired(current_run, decision)
            match decision:
                case CanContinue():
                    attempt_epoch = current_run.state.attempt_epoch + 1
                    await operation.repository.mark_attempt_started(
                        attempt_epoch=attempt_epoch
                    )
                    return AttemptStarted(attempt_epoch=attempt_epoch)
                case (
                    CancelledByUser()
                    | DeadlineExceeded()
                    | AttemptSuperseded() as stopped
                ):
                    return stopped

        return await self._with_locked_run(run_id, on_locked_run)

    async def authorize_stage_entry(
        self, run_id: UUID, *, attempt_epoch: int
    ) -> CanContinue | CannotContinue:
        async def on_locked_run(operation: _RunOperation, current_run: CurrentRun):
            decision = current_run.state.check_progress(
                attempt_epoch=attempt_epoch,
                phases={RunPhase.PREPARING},
                now=current_run.now,
            )
            await operation.close_if_expired(current_run, decision)
            return decision

        return await self._with_locked_run(run_id, on_locked_run)

    async def start_answer_generation(
        self, run_id: UUID, *, attempt_epoch: int
    ) -> AnswerGenerationStarted | CannotContinue:
        async def on_locked_run(operation: _RunOperation, current_run: CurrentRun):
            decision = current_run.state.check_progress(
                attempt_epoch=attempt_epoch,
                phases={RunPhase.PREPARING},
                now=current_run.now,
            )
            await operation.close_if_expired(current_run, decision)
            match decision:
                case CanContinue():
                    await operation.repository.mark_answer_started(
                        answer_started_at=current_run.now
                    )
                    return AnswerGenerationStarted(
                        run_id=run_id, answer_started_at=current_run.now
                    )
                case (
                    CancelledByUser()
                    | DeadlineExceeded()
                    | AttemptSuperseded() as stopped
                ):
                    return stopped

        return await self._with_locked_run(run_id, on_locked_run)

    async def authorize_answer_regeneration(
        self, run_id: UUID, *, attempt_epoch: int
    ) -> CanContinue | CannotContinue:
        async def on_locked_run(operation: _RunOperation, current_run: CurrentRun):
            decision = current_run.state.check_progress(
                attempt_epoch=attempt_epoch,
                phases={RunPhase.ANSWERING},
                now=current_run.now,
            )
            await operation.close_if_expired(current_run, decision)
            return decision

        return await self._with_locked_run(run_id, on_locked_run)

    async def complete(
        self,
        run_id: UUID,
        *,
        attempt_epoch: int,
        result: AnswerQuestionResult,
        research_handoff: dict[str, Any] | None,
    ) -> RunCompleted | CannotContinue:
        async def on_locked_run(operation: _RunOperation, current_run: CurrentRun):
            decision = current_run.state.check_attempt_finish(
                attempt_epoch=attempt_epoch,
                phases={RunPhase.ANSWERING},
                now=current_run.now,
            )
            await operation.close_if_expired(current_run, decision)
            match decision:
                case CanContinue():
                    pass
                case (
                    CancelledByUser()
                    | DeadlineExceeded()
                    | AttemptSuperseded() as stopped
                ):
                    return stopped

            repository = operation.repository
            warn_on_citation_source_mismatch(
                run_id=run_id,
                answer=result.answer,
                source_refs=[source.source_ref for source in result.sources],
            )
            assistant_message_id = await repository.add_assistant_message(result=result)
            await operation.close(
                current_run,
                status=AgentRunStatus.COMPLETED,
                assistant_message_id=assistant_message_id,
            )
            repository.touch_thread(updated_at=current_run.now)
            # Noneは「記録を追加しなかったRun」であり、既存handoffを消さない。
            if research_handoff is not None:
                repository.replace_research_handoff(research_handoff)
            return RunCompleted()

        return await self._with_locked_run(run_id, on_locked_run, lock_thread=True)

    async def record_failure(
        self,
        run_id: UUID,
        *,
        attempt_epoch: int,
        error_code: AgentRunErrorCode,
    ) -> FailureRecorded | CannotContinue:
        async def on_locked_run(operation: _RunOperation, current_run: CurrentRun):
            decision = current_run.state.check_attempt_finish(
                attempt_epoch=attempt_epoch,
                phases={RunPhase.PREPARING, RunPhase.ANSWERING},
                now=current_run.now,
            )
            await operation.close_if_expired(current_run, decision)
            match decision:
                case CanContinue():
                    await operation.close(
                        current_run, status=AgentRunStatus.FAILED, error_code=error_code
                    )
                    return FailureRecorded()
                case (
                    CancelledByUser()
                    | DeadlineExceeded()
                    | AttemptSuperseded() as stopped
                ):
                    return stopped

        return await self._with_locked_run(run_id, on_locked_run)

    async def cancel_for_user(
        self, run_id: UUID, *, user_id: UUID
    ) -> (
        CancelledByUser
        | DeadlineExceeded
        | RunAlreadyCompleted
        | RunAlreadyFailed
        | RunNotFound
    ):
        async def on_locked_run(operation: _RunOperation, current_run: CurrentRun):
            decision = current_run.state.check_cancel(now=current_run.now)
            await operation.close_if_expired(current_run, decision)
            match decision:
                case CanContinue():
                    await operation.close(
                        current_run,
                        status=AgentRunStatus.FAILED,
                        error_code=AgentRunErrorCode.CANCELLED,
                    )
                    return CancelledByUser()
                case (
                    CancelledByUser()
                    | DeadlineExceeded()
                    | RunAlreadyCompleted()
                    | RunAlreadyFailed()
                ) as ended:
                    return ended

        return await self._with_locked_run(run_id, on_locked_run, owner_id=user_id)

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
        async def on_locked_run(operation: _RunOperation, current_run: CurrentRun):
            decision = current_run.state.check_enqueue_failure(now=current_run.now)
            await operation.close_if_expired(current_run, decision)
            match decision:
                case CanContinue():
                    await operation.close(
                        current_run,
                        status=AgentRunStatus.FAILED,
                        error_code=AgentRunErrorCode.ENQUEUE_FAILED,
                    )
                    return FailureRecorded()
                case (
                    AttemptAlreadyStarted()
                    | CancelledByUser()
                    | DeadlineExceeded()
                    | RunAlreadyCompleted()
                    | RunAlreadyFailed()
                ) as skipped:
                    return skipped

        return await self._with_locked_run(run_id, on_locked_run)

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

    async def _with_locked_run[Result](
        self,
        run_id: UUID,
        on_locked_run: Callable[[_RunOperation, CurrentRun], Awaitable[Result]],
        *,
        owner_id: UUID | None = None,
        lock_thread: bool = False,
    ) -> Result | RunNotFound:
        """on_locked_runは一時的なDBの失敗で最初から呼び直すので、閉じたRunの通知はcommitの後にここで行う。"""

        async def work(
            session: AsyncSession,
        ) -> tuple[Result | RunNotFound, ClosedRun | None]:
            operation = _RunOperation(
                run_id=run_id,
                repository=AgentRunStateRepository(session, now=self._now),
            )
            match await operation.repository.lock(
                run_id, owner_id=owner_id, lock_thread=lock_thread
            ):
                case RunNotFound() as missing:
                    return missing, None
                case CurrentRun() as current_run:
                    return (
                        await on_locked_run(operation, current_run),
                        operation.closed_run,
                    )

        result, closed_run = await self._in_transaction(work)
        if closed_run is not None:
            await notify_closed_run(closed_run, publisher_for=self._publisher_for)
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


@dataclass(slots=True)
class _RunOperation:
    """1回の操作の間だけ使う、書き込みの口と、commit後に通知する閉じたRun。"""

    run_id: UUID
    repository: AgentRunStateRepository
    closed_run: ClosedRun | None = None

    async def close_if_expired(self, current_run: CurrentRun, decision: object) -> None:
        """期限切れの判定は既に閉じたRunにも返るので、まだ終わっていないRunだけを閉じる。"""
        match decision:
            case DeadlineExceeded() if current_run.state.phase is not RunPhase.FINISHED:
                await self.close(current_run, status=AgentRunStatus.DEADLINE_EXCEEDED)

    async def close(
        self,
        current_run: CurrentRun,
        *,
        status: AgentRunStatus,
        error_code: AgentRunErrorCode | None = None,
        assistant_message_id: UUID | None = None,
    ) -> None:
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
            run_id=self.run_id,
            attempt_epoch=current_run.state.attempt_epoch,
            status=status,
            error_code=error_code,
        )
