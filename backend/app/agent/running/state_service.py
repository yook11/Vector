"""Runの状態を変える操作を、条件を満たすRunにだけ1回のトランザクションで書き、commit後に閉じたRunを通知する。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from datetime import datetime
from functools import wraps
from typing import Any
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.contract import AnswerQuestionResult
from app.agent.live_updates.stream import (
    AgentRunLiveStreamPublisher,
    AgentRunLiveStreamTerminalEvent,
)
from app.agent.run_enqueuer import AgentRunEnqueuer
from app.agent.running.state import (
    ActiveRunExists,
    AnswerGenerationStarted,
    AttemptStarted,
    CancelledByUser,
    CanContinue,
    CannotContinue,
    DailyLimitReached,
    RunCompleted,
    RunCreated,
    RunFailed,
    RunLeftQueuedError,
    RunNotFound,
    ThreadNotFound,
)
from app.agent.running.state_repository import AgentRunStateRepository
from app.agent.running.types import AgentRunErrorCode, AgentRunStatus, RunAttempt
from app.db.retry_prospect import is_resolvable_by_retry
from app.db.translate import translate_database_error
from app.log_policy.policies.research import RESEARCH_LOG_RULES
from app.log_policy.runtime import create_policy_json_logger

__all__ = ["AgentRunStateService"]

logger = create_policy_json_logger(__name__, RESEARCH_LOG_RULES)

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
        publisher_for: Callable[[RunAttempt], AgentRunLiveStreamPublisher],
        now: datetime | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._publisher_for = publisher_for
        # テストでDBの時刻の代わりに使う。
        self._now = now

    async def create_run(
        self,
        *,
        user_id: UUID,
        question: str,
        thread_id: UUID | None,
        enqueuer: AgentRunEnqueuer,
        schedule_deadline_check: Callable[[UUID, datetime], None],
    ) -> RunCreated | ThreadNotFound | ActiveRunExists | DailyLimitReached:
        """ユーザーの質問からRunを作り、キューへ投入して、期限の確認を予約する。"""
        result = await self._create_queued_run(
            user_id=user_id, question=question, thread_id=thread_id
        )
        match result:
            case ThreadNotFound() | ActiveRunExists():
                return result
            case DailyLimitReached(usage_date=usage_date):
                logger.info(
                    "agent_run_daily_limit_reached",
                    usage_date=usage_date.isoformat(),
                )
                return result
            case RunCreated(
                run_id=run_id,
                deadline_at=deadline_at,
                usage_date=usage_date,
                used_count=used_count,
            ):
                logger.info(
                    "agent_run_accepted",
                    run_id=str(run_id),
                    usage_date=usage_date.isoformat(),
                    used_count=used_count,
                )
                try:
                    await enqueuer.enqueue(run_id)
                except Exception:
                    logger.exception("agent_run_enqueue_failed", run_id=str(run_id))
                    try:
                        await self.end_run_after_enqueue_failure(run_id)
                    except Exception as exc:
                        logger.exception(
                            "agent_run_enqueue_failed_mark_failed_failed",
                            run_id=str(run_id),
                        )
                        raise RunLeftQueuedError from exc
                schedule_deadline_check(run_id, deadline_at)
                return result

    @_retry_transient_database_failure
    async def _create_queued_run(
        self, *, user_id: UUID, question: str, thread_id: UUID | None
    ) -> RunCreated | ThreadNotFound | ActiveRunExists | DailyLimitReached:
        """やり直すのはRunを作る書き込みまでで、キューへの投入はやり直さない。"""
        async with self._transaction() as session:
            result = await self._repository(session).create_run(
                owner_id=user_id, question=question, thread_id=thread_id
            )
        return result

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
        await self._notify_run_ended(attempt, status=AgentRunStatus.COMPLETED)
        return RunCompleted()

    @_retry_transient_database_failure
    async def end_run_after_attempt_failure(
        self, attempt: RunAttempt, *, error_code: AgentRunErrorCode
    ) -> RunFailed | CannotContinue:
        async with self._transaction() as session:
            failed = await self._repository(session).end_run_of_attempt(
                attempt, error_code=error_code
            )
        if not failed:
            return CannotContinue()
        await self._notify_run_ended(
            attempt, status=AgentRunStatus.FAILED, error_code=error_code
        )
        return RunFailed()

    @_retry_transient_database_failure
    async def cancel_for_user(
        self, run_id: UUID, *, user_id: UUID
    ) -> CancelledByUser | CannotContinue | RunNotFound:
        async with self._transaction() as session:
            repository = self._repository(session)
            cancelled = await repository.cancel(run_id, owner_id=user_id)
            if cancelled is None:
                if not await repository.exists_for_owner(run_id, owner_id=user_id):
                    return RunNotFound()
                return CannotContinue()
        if cancelled.running_attempt is not None:
            await self._notify_run_ended(
                cancelled.running_attempt,
                status=AgentRunStatus.FAILED,
                error_code=AgentRunErrorCode.CANCELLED,
            )
        return CancelledByUser()

    @_retry_transient_database_failure
    async def end_run_after_enqueue_failure(
        self, run_id: UUID
    ) -> RunFailed | CannotContinue:
        async with self._transaction() as session:
            failed = await self._repository(session).end_queued_run(run_id)
        if not failed:
            return CannotContinue()
        return RunFailed()

    @_retry_transient_database_failure
    async def end_runs_past_deadline(
        self,
        *,
        run_id: UUID | None = None,
        thread_id: UUID | None = None,
        user_id: UUID | None = None,
    ) -> tuple[RunAttempt, ...]:
        async with self._transaction() as session:
            running_attempts = await self._repository(session).end_runs_past_deadline(
                run_id=run_id, thread_id=thread_id, owner_id=user_id
            )
        for attempt in running_attempts:
            await self._notify_run_ended(
                attempt, status=AgentRunStatus.DEADLINE_EXCEEDED
            )
        return running_attempts

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

    async def _notify_run_ended(
        self,
        attempt: RunAttempt,
        *,
        status: AgentRunStatus,
        error_code: AgentRunErrorCode | None = None,
    ) -> None:
        """commitの後に呼び、実行中だった試行の画面へ終了を通知する。"""
        event = AgentRunLiveStreamTerminalEvent.model_validate(
            {"status": status.value, "errorCode": error_code}
        )
        try:
            await self._publisher_for(attempt).publish(event)
        except Exception:
            logger.warning(
                "agent_run_live_stream_terminal_publish_failed",
                run_id=str(attempt.run_id),
                terminal_status=event.status,
            )
