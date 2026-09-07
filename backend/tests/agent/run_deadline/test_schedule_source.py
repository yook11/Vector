"""実Redisで単発予約の永続化・消費を確認する。"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from taskiq import ScheduledTask, TaskiqScheduler
from taskiq.serializers import JSONSerializer

from app.config import settings
from app.queue.brokers import broker_agent
from app.queue.deadline_schedule import AgentDeadlineScheduleSource, _encode_datetime

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.xdist_group("redis"),
]


@pytest.mark.parametrize("past", [False, True])
async def test_reservation_survives_source_restart_and_is_deleted_after_send(
    past, monkeypatch
):
    prefix = f"test-deadline-{uuid4().hex}"

    def source():
        return AgentDeadlineScheduleSource(
            settings.redis_url,
            prefix=prefix,
            serializer=JSONSerializer(default=_encode_datetime),
            skip_past_schedules=False,
        )

    writer, reader = source(), source()
    redis = Redis.from_url(settings.redis_url)
    at = datetime.now(UTC) - timedelta(minutes=3) if past else datetime.now(UTC)
    task = ScheduledTask(
        schedule_id=str(uuid4()),
        task_name="check_agent_run_deadline",
        args=[{"run_id": str(uuid4())}],
        kwargs={},
        labels={},
        time=at,
    )
    try:
        await writer.add_schedule(task)
        await writer.shutdown()
        await reader.startup()
        tasks = await reader.get_schedules()
        assert tasks == [task]
        assert await redis.llen(reader._get_time_key(at)) == 1
        raw = await redis.get(reader._get_data_key(task.schedule_id))
        assert raw.startswith(b"{")
        kick = AsyncMock()
        monkeypatch.setattr(broker_agent, "kick", kick)
        await TaskiqScheduler(broker_agent, [reader]).on_ready(reader, task)
        kick.assert_awaited_once()
        assert await reader.get_schedules() == []
        assert await redis.exists(reader._get_data_key(task.schedule_id)) == 0
        assert await redis.exists(reader._get_time_key(at)) == 0
    finally:
        keys = [key async for key in redis.scan_iter(f"{prefix}:*")]
        if keys:
            await redis.delete(*keys)
        await writer.shutdown()
        await reader.shutdown()
        await redis.aclose()


async def test_schedule_registration_buffers_both_writes_until_redis_transaction(
    monkeypatch,
):
    # 実Redisへの送信直前にはどちらのキーもなく、MULTI/EXECで同時登録する。
    from redis.asyncio.client import Pipeline

    prefix = f"test-deadline-{uuid4().hex}"
    source = AgentDeadlineScheduleSource(
        settings.redis_url,
        prefix=prefix,
        serializer=JSONSerializer(default=_encode_datetime),
        skip_past_schedules=False,
    )
    redis = Redis.from_url(settings.redis_url)
    task = ScheduledTask(
        schedule_id=str(uuid4()),
        task_name="check_agent_run_deadline",
        args=[],
        kwargs={},
        labels={},
        time=datetime.now(UTC),
    )
    execute = Pipeline.execute
    observed = []

    async def verify_transaction(pipeline, *args, **kwargs):
        assert pipeline.is_transaction
        assert len(pipeline.command_stack) == 2
        assert await redis.exists(source._get_data_key(task.schedule_id)) == 0
        assert await redis.exists(source._get_time_key(task.time)) == 0
        observed.append(True)
        return await execute(pipeline, *args, **kwargs)

    monkeypatch.setattr(Pipeline, "execute", verify_transaction)
    try:
        await source.add_schedule(task)
        assert observed == [True]
        assert await redis.exists(source._get_data_key(task.schedule_id)) == 1
        assert await redis.lrange(source._get_time_key(task.time), 0, -1) == [
            task.schedule_id.encode()
        ]
        await source.shutdown()
        assert all(
            not conn.is_connected
            for conn in source._connection_pool._available_connections
        )
    finally:
        keys = [key async for key in redis.scan_iter(f"{prefix}:*")]
        if keys:
            await redis.delete(*keys)
        await source.shutdown()
        await redis.aclose()
