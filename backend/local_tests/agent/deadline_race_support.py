"""回答確定と期限回収のトランザクションを保持し、実DBのロック待ちを観測する。"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.running.deadline.deadline_exceeded import _recover_deadline_exceeded_runs
from app.db.session import SessionFactory
from app.models.agent_message import AgentMessage, AgentMessageSource
from app.models.agent_run import AgentRun
from local_tests.agent.support import SavedRunResult, state_service_for


@dataclass
class PendingRunTransaction:
    session_factory: SessionFactory
    session: AsyncSession
    backend_pid: int
    operation: asyncio.Task

    async def wait_for_result(self):
        return await asyncio.wait_for(asyncio.shield(self.operation), timeout=5)

    async def wait_until_blocked_by(self, holder):
        await _wait_until_blocked(self.session_factory, waiter=self, holder=holder)

    async def finish(self):
        """ワーカーと同じく、拒否された結果も取り消さずにトランザクションを確定する。"""
        result = await self.wait_for_result()
        await self.session.commit()
        return result


class _CommitGate:
    """service が開くセッションと接続を記録し、commit をテストが開けるまで止める。"""

    def __init__(self, session_factory):
        self._session_factory = session_factory
        self.session = None
        self.backend_pid = None
        self.reached = asyncio.Event()
        self._opened = asyncio.Event()

    @asynccontextmanager
    async def __call__(self):
        async with self._session_factory() as session:
            begin = session.begin
            commit = session.commit

            async def begin_and_record_backend_pid():
                await begin()
                self.backend_pid = await session.scalar(select(func.pg_backend_pid()))

            async def gated_commit():
                self.reached.set()
                await self._opened.wait()
                await commit()

            session.begin = begin_and_record_backend_pid
            session.commit = gated_commit
            self.session = session
            yield session

    def open(self):
        self._opened.set()


@dataclass
class PendingRunCompletion:
    """回答確定をcommitの直前で止め、その間の保存内容とロックの競合を観測する。"""

    session_factory: SessionFactory
    commit_gate: _CommitGate
    operation: asyncio.Task

    @property
    def backend_pid(self):
        return self.commit_gate.backend_pid

    async def wait_until_ready_to_commit(self):
        async with asyncio.timeout(5):
            while not self.commit_gate.reached.is_set():
                if self.operation.done():
                    await self.operation
                    raise RuntimeError("commitの前に処理が終了しました")
                await asyncio.sleep(0.01)

    async def wait_until_blocked_by(self, holder):
        await _wait_until_blocked(self.session_factory, waiter=self, holder=holder)

    async def finish(self):
        """止めていたcommitを許し、回答確定の結果を返す。"""
        self.commit_gate.open()
        return await asyncio.wait_for(asyncio.shield(self.operation), timeout=5)

    async def snapshot(self, *, run_id, thread_id):
        """未commitの回答も含め、競合相手を再開する前の保存内容を保持する。"""
        await self.wait_until_ready_to_commit()
        session = self.commit_gate.session
        if session is None:
            raise RuntimeError("回答確定のセッションが開かれていません")
        run = await session.execute(
            select(
                AgentRun.id,
                AgentRun.status,
                AgentRun.assistant_message_id,
                AgentRun.error_code,
                AgentRun.attempt_epoch,
                AgentRun.answer_started_at,
            ).where(AgentRun.id == run_id, AgentRun.thread_id == thread_id)
        )
        messages = await session.execute(
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
        sources = await session.execute(
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


async def _wait_until_blocked(session_factory, *, waiter, holder):
    async with session_factory() as observer:
        # 回答保存側の本番lock_timeout（3秒）より先に観測の失敗を報告する。
        async with asyncio.timeout(2):
            while True:
                if waiter.operation.done():
                    await waiter.operation
                    raise RuntimeError("ロック待ちに入る前に処理が終了しました")
                if waiter.backend_pid is not None:
                    blockers = await observer.scalar(
                        select(func.pg_blocking_pids(waiter.backend_pid))
                    )
                    if holder.backend_pid in blockers:
                        return
                await asyncio.sleep(0.01)


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


@asynccontextmanager
async def pending_answer_completion(session_factory, *, run, answer, at):
    commit_gate = _CommitGate(session_factory)
    task = asyncio.create_task(
        state_service_for(commit_gate, now=at).complete(
            run.run_id,
            attempt_epoch=run.attempt_epoch,
            result=answer,
            research_handoff=None,
        )
    )
    try:
        yield PendingRunCompletion(session_factory, commit_gate, task)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def pending_deadline_recovery(session_factory, *, run_id, at):
    return _pending_transaction(
        session_factory,
        lambda session: _recover_deadline_exceeded_runs(
            session, thread_id=None, run_id=run_id, clock=literal(at)
        ),
    )
