"""期限予約の丸めと、回答から独立した失敗処理。"""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from structlog.testing import capture_logs
from taskiq import ScheduledTask, ScheduleSource

from app.agent.running.deadline import scheduling
from app.agent.running.deadline.scheduling import (
    AgentDeadlineScheduler,
    deadline_check_time,
)


class RecordingSource(ScheduleSource):
    def __init__(self, error=None, stall=False):
        self.schedules = []
        self.error = error
        self.stall = stall

    async def get_schedules(self):
        return self.schedules

    async def add_schedule(self, schedule: ScheduledTask):
        self.schedules.append(schedule)
        if self.error:
            raise self.error
        if self.stall:
            await asyncio.Event().wait()


@pytest.mark.parametrize(
    "at,expected",
    [
        ("2026-09-07T12:00:00+00:00", "2026-09-07T12:00:00+00:00"),
        ("2026-09-07T12:00:00.000001+00:00", "2026-09-07T12:00:01+00:00"),
        ("2026-09-07T23:59:59.999999+00:00", "2026-09-08T00:00:00+00:00"),
        ("2026-09-07T12:00:00.350000+09:00", "2026-09-07T03:00:01+00:00"),
    ],
)
def test_only_scheduled_time_is_rounded(at, expected):
    assert deadline_check_time(datetime.fromisoformat(at)) == datetime.fromisoformat(
        expected
    )


@pytest.mark.asyncio
async def test_saved_deadline_is_scheduled_even_when_in_past():
    source = RecordingSource()
    run_id = uuid4()
    deadline = datetime(2000, 1, 1, tzinfo=UTC)
    await AgentDeadlineScheduler(source).reserve(run_id, deadline)
    assert len(source.schedules) == 1
    task = source.schedules[0]
    assert task.time == deadline
    assert task.args == [{"run_id": str(run_id)}]
    assert task.task_name == "check_agent_run_deadline"
    assert task.cron is None and task.interval is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stall", [False, True])
async def test_failed_reservation_does_not_retry_or_propagate(monkeypatch, stall):
    monkeypatch.setattr(scheduling, "DEADLINE_RESERVATION_TIMEOUT_SECONDS", 0.01)
    source = RecordingSource(
        stall=stall, error=None if stall else RuntimeError("private payload")
    )
    with capture_logs() as logs:
        await AgentDeadlineScheduler(source).reserve(uuid4(), datetime.now(UTC))
    assert len(source.schedules) == 1
    assert (
        len(logs) == 1 and logs[0]["event"] == "agent_run_deadline_reservation_failed"
    )
    assert "private payload" not in str(logs)


@pytest.mark.asyncio
async def test_cancellation_propagates():
    source = RecordingSource(error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await AgentDeadlineScheduler(source).reserve(uuid4(), datetime.now(UTC))


@pytest.mark.asyncio
async def test_parent_cancellation_does_not_cancel_reservation():
    # 回答側のキャンセル後も予約はワーカーが所有する。
    entered, release = asyncio.Event(), asyncio.Event()
    source = RecordingSource()
    original = source.add_schedule

    async def add(schedule):
        entered.set()
        await release.wait()
        await original(schedule)

    source.add_schedule = add
    scheduler = AgentDeadlineScheduler(source)

    async def answer():
        scheduler.reserve_in_background(uuid4(), datetime.now(UTC))
        await asyncio.Event().wait()

    task = asyncio.create_task(answer())
    await entered.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert len(scheduler._pending) == 1
    release.set()
    await asyncio.gather(*scheduler._pending)
    await asyncio.sleep(0)
    assert len(source.schedules) == 1
    assert not scheduler._pending


@pytest.mark.asyncio
async def test_worker_cleanup_cancels_reservations_before_closing_source():
    # 別resourceの終了失敗でも予約を回収してから接続を閉じる。
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.queue.lifecycle import _aclose_worker_resources

    source = RecordingSource(stall=True)
    scheduler = AgentDeadlineScheduler(source)
    scheduler.reserve_in_background(uuid4(), datetime.now(UTC))
    await asyncio.sleep(0)

    async def shutdown():
        assert not scheduler._pending

    source.shutdown = AsyncMock(side_effect=shutdown)
    state = SimpleNamespace(
        agent_deadline_source=source,
        agent_deadline_scheduler=scheduler,
        pipeline_control_redis=SimpleNamespace(
            aclose=AsyncMock(side_effect=RuntimeError("close failed"))
        ),
    )
    with pytest.raises(RuntimeError, match="close failed"):
        await _aclose_worker_resources(state)
    source.shutdown.assert_awaited_once()
