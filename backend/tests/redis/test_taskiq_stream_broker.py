"""共通brokerの配送回収条件を実Redisで確認する。"""

import asyncio
from contextlib import aclosing
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from app.config import settings
from app.redis.clients import taskiq_stream_connection
from app.redis.taskiq_stream_broker import create_taskiq_stream_broker

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
    pytest.mark.xdist_group("redis"),
]


async def test_recovery_does_not_take_deliveries_before_idle_timeout():
    """期限を過ぎた配送だけを回収し、期限前の配送は元の担当に残す。"""
    stream = f"test:broker:{uuid4().hex}"
    broker = create_taskiq_stream_broker(taskiq_stream_connection(settings), stream)
    broker.is_worker_process = True
    broker.block = 20
    group = broker.consumer_group_name
    initial_consumer = "initial-worker"

    async with Redis.from_url(settings.redis_url) as redis:
        try:
            await broker.startup()
            active_id = await redis.xadd(stream, {"data": b"still-running"})
            expired_id = await redis.xadd(stream, {"data": b"needs-recovery"})
            await redis.xreadgroup(group, initial_consumer, {stream: ">"}, count=2)
            await redis.xclaim(
                stream,
                group,
                initial_consumer,
                min_idle_time=0,
                message_ids=[expired_id],
                idle=broker.idle_timeout + 1,
            )

            async with aclosing(broker.listen()) as listener:
                recovered = await asyncio.wait_for(anext(listener), timeout=2)
                pending = await redis.xpending_range(stream, group, "-", "+", 2)

                assert recovered.data == b"needs-recovery"
                assert {
                    entry["message_id"]: entry["consumer"] for entry in pending
                } == {
                    active_id: initial_consumer.encode(),
                    expired_id: broker.consumer_name.encode(),
                }
                await recovered.ack()
        finally:
            await broker.shutdown()
            await redis.delete(stream, f"autoclaim:{group}:{stream}")
