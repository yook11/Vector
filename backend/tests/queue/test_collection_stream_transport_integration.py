"""実Redisでのcollection multi-Stream routing / ACK契約。"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any, Literal
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

Stage = Literal["acquisition", "completion"]

_PRODUCTION_STREAM_BY_STAGE: dict[Stage, str] = {
    "acquisition": "pipeline:acquisition",
    "completion": "pipeline:completion",
}
_TASK_BY_STAGE: dict[Stage, tuple[str, str]] = {
    "acquisition": ("app.queue.tasks.acquisition", "acquire_source"),
    "completion": ("app.queue.tasks.completion", "scrape_html_body"),
}
_PAYLOAD_BY_STAGE: dict[Stage, dict[str, object]] = {
    "acquisition": {"arg": {"id": 1, "name": "hacker_news"}},
    "completion": {"incomplete_article_id": 1},
}


@dataclass(frozen=True)
class CollectionTransport:
    """1 test専用のcollection brokerと一意なRedis key集合。"""

    redis: Redis
    broker: RedisStreamBroker
    acquisition_stream: str
    completion_stream: str
    group: str = "taskiq"

    def stream_for(self, stage: Stage) -> str:
        if stage == "acquisition":
            return self.acquisition_stream
        return self.completion_stream

    def lock_for(self, stage: Stage) -> str:
        return f"autoclaim:{self.group}:{self.stream_for(stage)}"


@pytest.fixture
async def collection_transport() -> AsyncIterator[CollectionTransport]:
    """production batch/lock値を保ち、idle/blockだけ短縮した一意broker。"""
    suffix = uuid4().hex
    redis = aioredis.from_url(settings.redis_url)
    acquisition_stream = f"test:pipeline:acquisition:transport:{suffix}"
    completion_stream = f"test:pipeline:completion:transport:{suffix}"
    broker = RedisStreamBroker(
        url=settings.redis_url,
        queue_name=acquisition_stream,
        additional_streams={completion_stream: ">"},
        consumer_group_name="taskiq",
        consumer_id="0-0",
        maxlen=10_000,
        xread_block=20,
        idle_timeout=50,
        unacknowledged_batch_size=100,
        unacknowledged_lock_timeout=60,
    )
    transport = CollectionTransport(
        redis=redis,
        broker=broker,
        acquisition_stream=acquisition_stream,
        completion_stream=completion_stream,
    )
    try:
        yield transport
    finally:
        await broker.shutdown()
        await redis.delete(
            acquisition_stream,
            completion_stream,
            transport.lock_for("acquisition"),
            transport.lock_for("completion"),
        )
        await redis.aclose()


def _registered_task(stage: Stage) -> Any:
    module_name, attribute = _TASK_BY_STAGE[stage]
    return getattr(importlib.import_module(module_name), attribute)


def _taskiq_message(
    transport: CollectionTransport,
    stage: Stage,
    *,
    task_id: str | None = None,
    label_overrides: dict[str, object] | None = None,
) -> TaskiqMessage:
    """production task labelsをoracleに、一意Streamへ写したvalid messageを作る。"""
    registered_task = _registered_task(stage)
    production_route = registered_task.labels["queue_name"]
    oracle_stage = next(
        name
        for name, stream in _PRODUCTION_STREAM_BY_STAGE.items()
        if stream == production_route
    )
    labels = {
        **registered_task.labels,
        "queue_name": transport.stream_for(oracle_stage),
        **(label_overrides or {}),
    }
    return TaskiqMessage(
        task_id=task_id or uuid4().hex,
        task_name=registered_task.task_name,
        labels=labels,
        args=[],
        kwargs=_PAYLOAD_BY_STAGE[stage],
    )


async def _enqueue(
    transport: CollectionTransport,
    stage: Stage,
) -> TaskiqMessage:
    message = _taskiq_message(transport, stage)
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


async def _pending_count(transport: CollectionTransport, stage: Stage) -> int:
    summary = await transport.redis.xpending(
        transport.stream_for(stage), transport.group
    )
    return int(summary["pending"])


async def test_one_listener_consumes_prestartup_messages_and_acks_source_streams(
    collection_transport: CollectionTransport,
) -> None:
    """0-0 groupはstartup前の両Stream messageを1 listenerで回収する。"""
    expected = {
        (await _enqueue(collection_transport, stage)).task_id
        for stage in ("acquisition", "completion")
    }
    await collection_transport.broker.startup()

    received: set[str] = set()
    async with _listener(collection_transport) as listener:
        for _ in range(2):
            delivery = await _next(listener)
            received.add(_decode(collection_transport, delivery).task_id)
            await delivery.ack()

    assert (
        received,
        await _pending_count(collection_transport, "acquisition"),
        await _pending_count(collection_transport, "completion"),
    ) == (expected, 0, 0)


@pytest.mark.parametrize("stage", ["acquisition", "completion"])
async def test_synthetic_retry_xadd_stays_on_originating_stage_stream(
    collection_transport: CollectionTransport,
    stage: Stage,
) -> None:
    """人工retry labelsは同じStreamへ戻るが、production handler失敗のtestではない。"""
    middleware = SimpleRetryMiddleware(default_retry_count=0)
    middleware.set_broker(collection_transport.broker)
    message = _taskiq_message(
        collection_transport,
        stage,
        label_overrides={"max_retries": 2, "retry_on_error": True},
    )
    result = TaskiqResult(
        is_err=True,
        return_value=None,
        execution_time=0,
        error=RuntimeError("test-local retry"),
    )

    await middleware.on_error(message, result, RuntimeError("test-local retry"))

    origin = collection_transport.stream_for(stage)
    other_stage: Stage = "completion" if stage == "acquisition" else "acquisition"
    rows = await collection_transport.redis.xrange(origin)
    retried = collection_transport.broker.formatter.loads(rows[0][1][b"data"])
    assert (
        await collection_transport.redis.xlen(origin),
        await collection_transport.redis.xlen(
            collection_transport.stream_for(other_stage)
        ),
        retried.labels["queue_name"],
        int(retried.labels["_retries"]),
    ) == (1, 0, origin, 1)
