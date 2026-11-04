"""共通brokerの配送の回収と、実行できない配送の扱いを実Redisで確認する。"""

import asyncio
from contextlib import aclosing
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from taskiq import AsyncBroker
from taskiq.message import TaskiqMessage

from app.config import settings
from app.redis.clients import taskiq_stream_connection
from app.redis.taskiq_stream_broker import create_taskiq_stream_broker

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
    pytest.mark.xdist_group("redis"),
]


async def _known_task() -> None:
    return None


def _encoded_message(broker: AsyncBroker, task_name: str) -> bytes:
    return broker.formatter.dumps(
        TaskiqMessage(
            task_id=uuid4().hex,
            task_name=task_name,
            labels={},
            args=[],
            kwargs={},
        )
    ).message


async def test_recovery_does_not_take_deliveries_before_idle_timeout():
    """期限を過ぎた配送だけを回収し、期限前の配送は元の担当に残す。"""
    stream = f"test:broker:{uuid4().hex}"
    broker = create_taskiq_stream_broker(taskiq_stream_connection(settings), stream)
    broker.is_worker_process = True
    broker.block = 20
    broker.register_task(_known_task, task_name="known_task")
    group = broker.consumer_group_name
    initial_consumer = "initial-worker"
    expired = _encoded_message(broker, "known_task")

    async with Redis.from_url(settings.redis_url) as redis:
        try:
            await broker.startup()
            active_id = await redis.xadd(
                stream, {"data": _encoded_message(broker, "known_task")}
            )
            expired_id = await redis.xadd(stream, {"data": expired})
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

                assert recovered.data == expired
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


async def test_unrunnable_new_deliveries_are_acked_without_reaching_worker():
    """読めない配送と知らないタスク名の配送はACKして渡さず、続く実行できる配送だけを渡す。"""
    stream = f"test:broker:{uuid4().hex}"
    broker = create_taskiq_stream_broker(taskiq_stream_connection(settings), stream)
    broker.is_worker_process = True
    broker.block = 20
    broker.register_task(_known_task, task_name="known_task")
    group = broker.consumer_group_name
    runnable = _encoded_message(broker, "known_task")

    async with Redis.from_url(settings.redis_url) as redis:
        try:
            await broker.startup()
            await redis.xadd(stream, {"data": b"not-a-taskiq-message"})
            await redis.xadd(stream, {"data": _encoded_message(broker, "renamed_task")})
            runnable_id = await redis.xadd(stream, {"data": runnable})

            async with aclosing(broker.listen()) as listener:
                delivered = await asyncio.wait_for(anext(listener), timeout=2)
                pending = await redis.xpending_range(stream, group, "-", "+", 3)

                assert (
                    delivered.data,
                    [entry["message_id"] for entry in pending],
                ) == (runnable, [runnable_id])
        finally:
            await broker.shutdown()
            await redis.delete(stream, f"autoclaim:{group}:{stream}")


async def test_unrunnable_recovered_deliveries_are_acked_without_reaching_worker():
    """期限を過ぎて回収した配送も、読めない・知らないタスク名ならACKして渡さず、実行できる配送だけを渡す。"""
    stream = f"test:broker:{uuid4().hex}"
    broker = create_taskiq_stream_broker(taskiq_stream_connection(settings), stream)
    broker.is_worker_process = True
    broker.block = 20
    broker.register_task(_known_task, task_name="known_task")
    group = broker.consumer_group_name
    initial_consumer = "initial-worker"
    runnable = _encoded_message(broker, "known_task")

    async with Redis.from_url(settings.redis_url) as redis:
        try:
            await broker.startup()
            unreadable_id = await redis.xadd(stream, {"data": b"not-a-taskiq-message"})
            unknown_id = await redis.xadd(
                stream, {"data": _encoded_message(broker, "renamed_task")}
            )
            runnable_id = await redis.xadd(stream, {"data": runnable})
            await redis.xreadgroup(group, initial_consumer, {stream: ">"}, count=3)
            await redis.xclaim(
                stream,
                group,
                initial_consumer,
                min_idle_time=0,
                message_ids=[unreadable_id, unknown_id, runnable_id],
                idle=broker.idle_timeout + 1,
            )

            async with aclosing(broker.listen()) as listener:
                recovered = await asyncio.wait_for(anext(listener), timeout=2)
                pending = await redis.xpending_range(stream, group, "-", "+", 3)

                assert (
                    recovered.data,
                    [entry["message_id"] for entry in pending],
                ) == (runnable, [runnable_id])
        finally:
            await broker.shutdown()
            await redis.delete(stream, f"autoclaim:{group}:{stream}")
