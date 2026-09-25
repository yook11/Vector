"""実Redisでのcollection Stream routing / ACK契約。"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest
from redis import asyncio as aioredis
from redis.asyncio import Redis
from taskiq import AckableMessage, SimpleRetryMiddleware, TaskiqResult
from taskiq.message import TaskiqMessage
from taskiq_redis import RedisStreamBroker

from app.config import settings

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
    pytest.mark.xdist_group("redis"),
]

_PRODUCTION_STREAM = "pipeline:acquisition"
_TASK = ("app.queue.tasks.acquisition", "acquire_source")
_PAYLOAD: dict[str, object] = {"arg": {"id": 1, "name": "hacker_news"}}


@dataclass(frozen=True)
class CollectionTransport:
    """1 test専用のcollection brokerと一意なRedis key集合。"""

    redis: Redis
    broker: RedisStreamBroker
    stream: str
    group: str = "taskiq"

    @property
    def lock(self) -> str:
        return f"autoclaim:{self.group}:{self.stream}"


@pytest.fixture
async def collection_transport() -> AsyncIterator[CollectionTransport]:
    """production batch/lock値を保ち、idle/blockだけ短縮した一意broker。"""
    suffix = uuid4().hex
    redis = aioredis.from_url(settings.redis_url)
    stream = f"test:pipeline:acquisition:transport:{suffix}"
    broker = RedisStreamBroker(
        url=settings.redis_url,
        queue_name=stream,
        consumer_group_name="taskiq",
        consumer_id="0-0",
        maxlen=10_000,
        xread_block=20,
        idle_timeout=50,
        unacknowledged_batch_size=100,
        unacknowledged_lock_timeout=60,
    )
    transport = CollectionTransport(redis=redis, broker=broker, stream=stream)
    try:
        yield transport
    finally:
        await broker.shutdown()
        await redis.delete(stream, transport.lock)
        await redis.aclose()


def _registered_task() -> Any:
    module_name, attribute = _TASK
    return getattr(importlib.import_module(module_name), attribute)


def _taskiq_message(
    transport: CollectionTransport,
    *,
    task_id: str | None = None,
    label_overrides: dict[str, object] | None = None,
) -> TaskiqMessage:
    """production task labelsをoracleに、一意Streamへ写したvalid messageを作る。"""
    registered_task = _registered_task()
    assert registered_task.labels["queue_name"] == _PRODUCTION_STREAM
    labels = {
        **registered_task.labels,
        "queue_name": transport.stream,
        **(label_overrides or {}),
    }
    return TaskiqMessage(
        task_id=task_id or uuid4().hex,
        task_name=registered_task.task_name,
        labels=labels,
        args=[],
        kwargs=_PAYLOAD,
    )


async def _enqueue(transport: CollectionTransport) -> TaskiqMessage:
    message = _taskiq_message(transport)
    await transport.broker.kick(transport.broker.formatter.dumps(message))
    return message


def _decode(transport: CollectionTransport, message: AckableMessage) -> TaskiqMessage:
    return transport.broker.formatter.loads(message.data)


@asynccontextmanager
async def _listener(
    transport: CollectionTransport,
) -> AsyncIterator[AsyncGenerator[AckableMessage]]:
    listener = transport.broker.listen()
    try:
        yield listener
    finally:
        with suppress(RuntimeError):
            await listener.aclose()


async def _next(
    listener: AsyncGenerator[AckableMessage],
    *,
    timeout: float = 2,
) -> AckableMessage:
    return await asyncio.wait_for(anext(listener), timeout=timeout)


async def _pending_count(transport: CollectionTransport) -> int:
    summary = await transport.redis.xpending(transport.stream, transport.group)
    return int(summary["pending"])


async def test_listener_consumes_prestartup_message_and_acks_stream(
    collection_transport: CollectionTransport,
) -> None:
    """0-0 groupはstartup前に積まれたmessageを回収し、ACK後にpendingを残さない。"""
    expected = (await _enqueue(collection_transport)).task_id
    await collection_transport.broker.startup()

    async with _listener(collection_transport) as listener:
        delivery = await _next(listener)
        received = _decode(collection_transport, delivery).task_id
        await delivery.ack()

    assert (received, await _pending_count(collection_transport)) == (expected, 0)


async def test_synthetic_retry_xadd_stays_on_originating_stream(
    collection_transport: CollectionTransport,
) -> None:
    """人工retry labelsは同じStreamへ戻るが、production handler失敗のtestではない。"""
    middleware = SimpleRetryMiddleware(default_retry_count=0)
    middleware.set_broker(collection_transport.broker)
    message = _taskiq_message(
        collection_transport,
        label_overrides={"max_retries": 2, "retry_on_error": True},
    )
    result = TaskiqResult(
        is_err=True,
        return_value=None,
        execution_time=0,
        error=RuntimeError("test-local retry"),
    )

    await middleware.on_error(message, result, RuntimeError("test-local retry"))

    origin = collection_transport.stream
    rows = await collection_transport.redis.xrange(origin)
    retried = collection_transport.broker.formatter.loads(rows[0][1][b"data"])
    assert (
        await collection_transport.redis.xlen(origin),
        retried.labels["queue_name"],
        int(retried.labels["_retries"]),
    ) == (1, origin, 1)
