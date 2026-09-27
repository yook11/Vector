"""回答確定と期限回収のトランザクションを保持し、実DBのロック待ちを観測する。"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.running.completion import (
    AgentRunCompletionRepository,
    RunCompletionFailure,
    RunCompletionFailureReason,
)
from app.agent.running.deadline.deadline_exceeded import _recover_deadline_exceeded_runs
from app.db.session import SessionFactory
from app.models.agent_message import AgentMessage, AgentMessageSource
from app.models.agent_run import AgentRun
from local_tests.agent.support import SavedRunResult


@dataclass
class PendingRunTransaction:
    session_factory: SessionFactory
    session: AsyncSession
    backend_pid: int
    operation: asyncio.Task

    async def wait_for_result(self):
        return await asyncio.wait_for(asyncio.shield(self.operation), timeout=5)

    async def wait_until_blocked_by(self, holder):
        async with self.session_factory() as observer:
            # 回答保存側の本番lock_timeout（3秒）より先に観測の失敗を報告する。
            async with asyncio.timeout(2):
                while True:
                    if self.operation.done():
                        await self.operation
                        raise RuntimeError("ロック待ちに入る前に処理が終了しました")
                    blockers = await observer.scalar(
                        select(func.pg_blocking_pids(self.backend_pid))
                    )
                    if holder.backend_pid in blockers:
                        return
                    await asyncio.sleep(0.01)

    async def finish(self):
        """処理結果に応じてトランザクションを確定または取り消す。"""
        result = await self.wait_for_result()
        if (
            isinstance(result, RunCompletionFailure)
            and result.reason != RunCompletionFailureReason.DEADLINE_EXCEEDED
        ):
            await self.session.rollback()
        else:
            await self.session.commit()
        return result

    async def snapshot(self, *, run_id, thread_id):
        """未commitの回答も含め、競合相手を再開する前の保存内容を保持する。"""
        await self.wait_for_result()
        run = await self.session.execute(
            select(
                AgentRun.id,
                AgentRun.status,
                AgentRun.assistant_message_id,
                AgentRun.error_code,
                AgentRun.attempt_epoch,
                AgentRun.answer_started_at,
            ).where(AgentRun.id == run_id, AgentRun.thread_id == thread_id)
        )
        messages = await self.session.execute(
            select(
                AgentMessage.id,
                AgentMessage.seq,
                AgentMessage.content,
                AgentMessage.missing_aspects,
            )
            .where(
                AgentMessage.thread_id == thread_id, AgentMessage.role == "assistant"
            )
            .order_by(AgentMessage.seq)
        )
        sources = await self.session.execute(
            select(
                AgentMessageSource.id,
                AgentMessageSource.message_id,
                AgentMessageSource.ordinal,
                AgentMessageSource.kind,
                AgentMessageSource.source_ref,
                AgentMessageSource.analyzed_article_id,
                AgentMessageSource.url,
                AgentMessageSource.title,
                AgentMessageSource.source_name,
                AgentMessageSource.published_at,
                AgentMessageSource.evidence_claim,
            )
            .join(AgentMessage, AgentMessage.id == AgentMessageSource.message_id)
            .where(AgentMessage.thread_id == thread_id)
            .order_by(AgentMessage.seq, AgentMessageSource.ordinal)
        )
        return SavedRunResult(
            dict(run.mappings().one()),
            [dict(row) for row in messages.mappings()],
            [dict(row) for row in sources.mappings()],
        )


@asynccontextmanager
async def _pending_transaction(session_factory, operation):
    async with session_factory() as session:
        await session.begin()
        backend_pid = await session.scalar(select(func.pg_backend_pid()))
        task = asyncio.create_task(operation(session))
        try:
            yield PendingRunTransaction(session_factory, session, backend_pid, task)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if session.in_transaction():
                await session.rollback()


def pending_answer_completion(session_factory, *, run, answer, at):
    return _pending_transaction(
        session_factory,
        lambda session: AgentRunCompletionRepository(session).complete_run(
            run_id=run.run_id,
            result=answer,
            expected_attempt_epoch=run.attempt_epoch,
            now=at,
        ),
    )


def pending_deadline_recovery(session_factory, *, run_id, at):
    return _pending_transaction(
        session_factory,
        lambda session: _recover_deadline_exceeded_runs(
            session, thread_id=None, run_id=run_id, clock=literal(at)
        ),
    )
