"""補完の確定直前を停止し、実SQL障害とトランザクションの状態を観測する。"""

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession


async def transaction_state(session, target):
    # flush後の同一接続で、取り消す前に保存操作が成立していたことを確認する。
    row = (
        (
            await session.execute(
                text(
                    "SELECT (SELECT status FROM incomplete_articles "
                    "WHERE id=:id) AS status, "
                    "(SELECT count(*) FROM analyzable_articles "
                    "WHERE source_url=:url) AS articles, "
                    "(SELECT count(id) FROM pipeline_events) AS audits, "
                    "(SELECT count(event_id) FROM outbox_events) AS outbox"
                ),
                {"id": target.id, "url": target.url},
            )
        )
        .mappings()
        .one()
    )
    return dict(row)


@dataclass
class CommitControl:
    reached: asyncio.Event = field(default_factory=asyncio.Event)
    released: asyncio.Event = field(default_factory=asyncio.Event)
    state: dict | None = None
    pid: int | None = None
    error: DBAPIError | None = None

    async def wait_reached(self):
        await asyncio.wait_for(self.reached.wait(), 10)

    def release(self):
        self.released.set()


@contextmanager
def hold_commit(monkeypatch, target, *, phase, fail=False):
    control = CommitControl()
    commit = AsyncSession.commit

    async def intercept(session):
        await session.flush()
        state = await transaction_state(session, target)
        matched = {
            "completed": state["status"] is None and state["articles"] == 1,
            "closed": state["status"] == "closed" and state["audits"] == 0,
            "failure_audit": state["audits"] > 0 and state["articles"] == 0,
        }[phase]
        if matched and control.state is None:
            control.state = state
            control.pid = await session.scalar(text("SELECT pg_backend_pid()"))
            control.reached.set()
            if fail:
                try:
                    await session.execute(text("SELECT 1 / 0"))
                except DBAPIError as exc:
                    control.error = exc
                    raise
            else:
                await asyncio.wait_for(control.released.wait(), 15)
        await commit(session)

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "commit", intercept)
        try:
            yield control
        finally:
            control.release()
