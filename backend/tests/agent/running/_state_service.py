"""操作を呼ぶ service と、commit後の通知の記録・commitの足止め。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.live_updates.stream import AgentRunLiveStreamTerminalEvent
from app.agent.running.daily_quota import observability as daily_quota_observability
from app.agent.running.daily_quota.release import DailyQuotaReleaseOutcome
from app.agent.running.finish_notice import (
    ClosedQueuedRun,
    ClosedRun,
    ClosedRunningRun,
)
from app.agent.running.state_service import AgentRunStateService
from app.agent.running.types import AgentRunStatus

__all__ = [
    "ClosedRunNotices",
    "CommitGate",
    "record_closed_runs",
    "state_service",
    "wait_until_lock_waiting",
]


@dataclass
class ClosedRunNotices:
    """commit後に届いた終了通知と利用枠の返却の観測を、閉じたRunとして届いた順に並べる。"""

    closed_runs: list[ClosedRun] = field(default_factory=list)

    def publisher_for(self, run_id: UUID, attempt_epoch: int) -> _RecordingPublisher:
        return _RecordingPublisher(self, run_id, attempt_epoch)

    def observe_release(
        self, *, run_id: UUID, outcome: DailyQuotaReleaseOutcome
    ) -> None:
        self.closed_runs.append(
            ClosedQueuedRun(run_id=run_id, quota_release_outcome=outcome)
        )


@dataclass
class _RecordingPublisher:
    notices: ClosedRunNotices
    run_id: UUID
    attempt_epoch: int

    async def publish(self, event: object) -> None:
        assert isinstance(event, AgentRunLiveStreamTerminalEvent)
        self.notices.closed_runs.append(
            ClosedRunningRun(
                run_id=self.run_id,
                attempt_epoch=self.attempt_epoch,
                status=AgentRunStatus(event.status),
                error_code=event.error_code,
            )
        )


def record_closed_runs(monkeypatch: pytest.MonkeyPatch) -> ClosedRunNotices:
    """利用枠の返却の観測を差し替え、終了通知と合わせて記録する。"""
    notices = ClosedRunNotices()
    monkeypatch.setattr(
        daily_quota_observability, "observe_release", notices.observe_release
    )
    return notices


def state_service(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    notices: ClosedRunNotices | None = None,
    now: datetime | None = None,
) -> AgentRunStateService:
    return AgentRunStateService(
        session_factory=session_factory,
        publisher_for=(notices or ClosedRunNotices()).publisher_for,  # type: ignore[arg-type]
        now=now,
    )


class CommitGate:
    """service が開くセッションの commit を、テストが open するまで止める。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self.reached = asyncio.Event()
        self._opened = asyncio.Event()

    def __call__(self) -> AsyncSession:
        session = self._session_factory()
        commit = session.commit

        async def gated_commit() -> None:
            self.reached.set()
            await self._opened.wait()
            await commit()

        session.commit = gated_commit  # type: ignore[method-assign]
        return session

    def open(self) -> None:
        self._opened.set()


async def wait_until_lock_waiting(observer: AsyncSession) -> None:
    """service の接続の pid は見えないため、ロックを待つ接続が現れるまで待つ。"""
    async with asyncio.timeout(5):
        while True:
            await observer.execute(text("SELECT pg_stat_clear_snapshot()"))
            is_waiting_for_lock = await observer.scalar(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM pg_stat_activity
                        WHERE datname = current_database()
                          AND wait_event_type = 'Lock'
                          AND cardinality(pg_blocking_pids(pid)) > 0
                    )
                    """
                )
            )
            if is_waiting_for_lock:
                return
            await asyncio.sleep(0.01)
