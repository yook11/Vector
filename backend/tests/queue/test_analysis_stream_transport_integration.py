"""実Redisでのanalysis multi-Stream transport / recovery契約。"""

from __future__ import annotations

import asyncio
import importlib
from collections import Counter
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

Stage = Literal["curation", "assessment"]

_PRODUCTION_STREAM_BY_STAGE: dict[Stage, str] = {
    "curation": "pipeline:curation",
    "assessment": "pipeline:assessment",
}
_TASK_BY_STAGE: dict[Stage, tuple[str, str]] = {
    "curation": ("app.queue.tasks.curation", "curate_content"),
    "assessment": ("app.queue.tasks.assessment", "assess_content"),
}
_TRIGGER_BY_STAGE: dict[Stage, dict[str, int]] = {
    "curation": {"analyzable_article_id": 1},
    "assessment": {"curation_id": 1},
}


@dataclass(frozen=True)
class AnalysisTransport:
    """1 test専用のanalysis brokerと一意なRedis key集合。"""

    redis: Redis
    broker: RedisStreamBroker
    curation_stream: str
    assessment_stream: str
    group: str = "taskiq"

    def stream_for(self, stage: Stage) -> str:
        if stage == "curation":
            return self.curation_stream
        return self.assessment_stream

    def lock_for(self, stage: Stage) -> str:
        return f"autoclaim:{self.group}:{self.stream_for(stage)}"


@pytest.fixture
async def analysis_transport() -> AsyncIterator[AnalysisTransport]:
    """production batch/lock値を保ち、idle/blockだけ短縮した一意broker。"""
    suffix = uuid4().hex
    redis = aioredis.from_url(settings.redis_url)
    curation_stream = f"test:pipeline:curation:transport:{suffix}"
    assessment_stream = f"test:pipeline:assessment:transport:{suffix}"
    broker = RedisStreamBroker(
        url=settings.redis_url,
        queue_name=curation_stream,
        additional_streams={assessment_stream: ">"},
        consumer_group_name="taskiq",
        consumer_id="0-0",
        maxlen=10_000,
        xread_block=20,
        idle_timeout=50,
        unacknowledged_batch_size=100,
        unacknowledged_lock_timeout=60,
    )
    transport = AnalysisTransport(
        redis=redis,
        broker=broker,
        curation_stream=curation_stream,
        assessment_stream=assessment_stream,
    )
    try:
        yield transport
    finally:
        await broker.shutdown()
        await redis.delete(
            curation_stream,
            assessment_stream,
            transport.lock_for("curation"),
            transport.lock_for("assessment"),
        )
        await redis.aclose()


def _registered_task(stage: Stage) -> Any:
    module_name, attribute = _TASK_BY_STAGE[stage]
    return getattr(importlib.import_module(module_name), attribute)


def _taskiq_message(
    transport: AnalysisTransport,
    stage: Stage,
    *,
    task_id: str | None = None,
    label_overrides: dict[str, object] | None = None,
) -> TaskiqMessage:
    """production task labelをoracleに、一意Streamへ写したvalid messageを作る。"""
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
        kwargs={"trigger": _TRIGGER_BY_STAGE[stage]},
    )


async def _enqueue(
    transport: AnalysisTransport,
    stage: Stage,
    *,
    task_id: str | None = None,
) -> TaskiqMessage:
    message = _taskiq_message(transport, stage, task_id=task_id)
    await transport.broker.kick(transport.broker.formatter.dumps(message))
    return message


def _decode(transport: AnalysisTransport, message: AckableMessage) -> TaskiqMessage:
    return transport.broker.formatter.loads(message.data)


@asynccontextmanager
async def _listener(
    transport: AnalysisTransport,
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


async def _pending_count(transport: AnalysisTransport, stage: Stage) -> int:
    summary = await transport.redis.xpending(
        transport.stream_for(stage), transport.group
    )
    return int(summary["pending"])


async def _wait_for_pending_change(
    transport: AnalysisTransport,
    stage: Stage,
    initial: int,
) -> int:
    """receiverのRedis scan完了を固定回数の短いpollで待つ。"""
    for _ in range(100):
        current = await _pending_count(transport, stage)
        if current != initial:
            return current
        await asyncio.sleep(0.01)
    raise AssertionError(f"{stage} pending did not change from {initial}")


async def _seed_stale_pending(
    transport: AnalysisTransport,
    stage: Stage,
    count: int,
    *,
    consumer: str = "seed-consumer",
) -> list[bytes]:
    """valid payloadをPELへ配達し、clock sleepなしでidleを1秒に固定する。"""
    message = _taskiq_message(transport, stage)
    payload = transport.broker.formatter.dumps(message).message
    stream = transport.stream_for(stage)
    pipeline = transport.redis.pipeline(transaction=False)
    for _ in range(count):
        pipeline.xadd(stream, {b"data": payload})
    await pipeline.execute()

    delivered = await transport.redis.xreadgroup(
        transport.group,
        consumer,
        {stream: ">"},
        count=count,
    )
    message_ids = [message_id for message_id, _ in delivered[0][1]]
    await transport.redis.xclaim(
        stream,
        transport.group,
        consumer,
        min_idle_time=0,
        message_ids=message_ids,
        idle=1_000,
    )
    return message_ids


async def test_one_listener_consumes_prestartup_messages_and_acks_source_streams(
    analysis_transport: AnalysisTransport,
) -> None:
    """0-0 groupはstartup前の両Stream messageを1 listenerで回収する。"""
    expected = {
        (await _enqueue(analysis_transport, stage)).task_id
        for stage in ("curation", "assessment")
    }
    await analysis_transport.broker.startup()

    received: set[str] = set()
    async with _listener(analysis_transport) as listener:
        for _ in range(2):
            delivery = await _next(listener)
            received.add(_decode(analysis_transport, delivery).task_id)
            await delivery.ack()

    assert (
        received,
        await _pending_count(analysis_transport, "curation"),
        await _pending_count(analysis_transport, "assessment"),
    ) == (expected, 0, 0)


@pytest.mark.parametrize("stage", ["curation", "assessment"])
async def test_simple_retry_xadd_stays_on_originating_stage_stream(
    analysis_transport: AnalysisTransport,
    stage: Stage,
) -> None:
    """SimpleRetryは受信queue_nameを保ち、実Redisの同じStreamへ再投入する。"""
    middleware = SimpleRetryMiddleware(default_retry_count=0)
    middleware.set_broker(analysis_transport.broker)
    message = _taskiq_message(
        analysis_transport,
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

    origin = analysis_transport.stream_for(stage)
    other_stage: Stage = "assessment" if stage == "curation" else "curation"
    rows = await analysis_transport.redis.xrange(origin)
    retried = analysis_transport.broker.formatter.loads(rows[0][1][b"data"])
    assert (
        await analysis_transport.redis.xlen(origin),
        await analysis_transport.redis.xlen(analysis_transport.stream_for(other_stage)),
        retried.labels["queue_name"],
        int(retried.labels["_retries"]),
    ) == (1, 0, origin, 1)


async def test_stale_pel_is_claimed_only_after_wake_and_batch_is_capped_at_100(
    analysis_transport: AnalysisTransport,
) -> None:
    """新規配達がないiterationではscanせず、wake後に両Streamを最大100件ずつscanする。"""
    await analysis_transport.broker.startup()
    await _seed_stale_pending(analysis_transport, "curation", 101)
    await _seed_stale_pending(analysis_transport, "assessment", 1)

    waiting: asyncio.Task[AckableMessage] | None = None
    async with _listener(analysis_transport) as listener:
        try:
            waiting = asyncio.create_task(anext(listener))
            done, _ = await asyncio.wait({waiting}, timeout=0.12)
            assert not done, "stale PELだけでlistenerがmessageを返した"

            wake = await _enqueue(analysis_transport, "curation")
            wake_delivery = await asyncio.wait_for(waiting, timeout=2)
            assert _decode(analysis_transport, wake_delivery).task_id == wake.task_id
            await wake_delivery.ack()

            claimed: Counter[str] = Counter()
            for _ in range(101):
                delivery = await _next(listener)
                claimed[_decode(analysis_transport, delivery).labels["queue_name"]] += 1
                await delivery.ack()
        finally:
            if waiting is not None and not waiting.done():
                waiting.cancel()
                with suppress(asyncio.CancelledError):
                    await waiting

    remaining = await analysis_transport.redis.xpending_range(
        analysis_transport.curation_stream,
        analysis_transport.group,
        min="-",
        max="+",
        count=2,
    )
    assert (
        claimed,
        await _pending_count(analysis_transport, "curation"),
        await _pending_count(analysis_transport, "assessment"),
        {item["consumer"] for item in remaining},
    ) == (
        Counter(
            {
                analysis_transport.curation_stream: 100,
                analysis_transport.assessment_stream: 1,
            }
        ),
        1,
        0,
        {b"seed-consumer"},
    )


async def test_one_wake_cleans_at_most_100_ghost_pel_references(
    analysis_transport: AnalysisTransport,
) -> None:
    """COUNT=100で観測できるghost cleanupは100件までに留まる。

    Redis文書上のCOUNT*10=1,000は内部PEL scan上限であり、本テストが観測する
    deleted ghost ID数ではない。
    """
    await analysis_transport.broker.startup()
    ghost_ids = await _seed_stale_pending(analysis_transport, "curation", 1_001)
    deleted = await analysis_transport.redis.xdel(
        analysis_transport.curation_stream, *ghost_ids
    )
    wake = await _enqueue(analysis_transport, "assessment")

    scan: asyncio.Task[AckableMessage] | None = None
    async with _listener(analysis_transport) as listener:
        wake_delivery = await _next(listener)
        assert _decode(analysis_transport, wake_delivery).task_id == wake.task_id
        await wake_delivery.ack()

        try:
            scan = asyncio.create_task(anext(listener))
            remaining_ghosts = await asyncio.wait_for(
                _wait_for_pending_change(analysis_transport, "curation", 1_001),
                timeout=2,
            )
            yielded_live_payload = scan.done()
        finally:
            if scan is not None and not scan.done():
                scan.cancel()
                with suppress(asyncio.CancelledError):
                    await scan

    assert (
        deleted,
        yielded_live_payload,
        remaining_ghosts,
        await analysis_transport.redis.xlen(analysis_transport.curation_stream),
    ) == (1_001, False, 901, 0)


async def test_autoclaim_lock_primitive_uses_finite_production_timeout(
    analysis_transport: AnalysisTransport,
) -> None:
    """listenerと同じpipeline lock primitiveへ60秒TTLが設定される。"""
    pipeline = analysis_transport.redis.pipeline()
    lock = pipeline.lock(
        analysis_transport.lock_for("curation"),
        timeout=analysis_transport.broker.unacknowledged_lock_timeout,
    )
    try:
        acquired = await lock.acquire(blocking=False)
        set_results = await pipeline.execute()
        ttl_ms = await analysis_transport.redis.pttl(
            analysis_transport.lock_for("curation")
        )
        assert acquired and set_results == [True] and 0 < ttl_ms <= 60_000
    finally:
        await analysis_transport.redis.delete(analysis_transport.lock_for("curation"))


async def test_persistent_foreign_lock_survives_receiver_scan(
    analysis_transport: AnalysisTransport,
) -> None:
    """1.2.3はpipeline scanを進めるが、既存TTL=-1 token自体は削除しない。"""
    await analysis_transport.broker.startup()
    await _seed_stale_pending(analysis_transport, "curation", 1)
    lock_key = analysis_transport.lock_for("curation")
    foreign_token = b"foreign-persistent-lock"
    await analysis_transport.redis.set(lock_key, foreign_token)
    wake = await _enqueue(analysis_transport, "curation")

    async with _listener(analysis_transport) as listener:
        wake_delivery = await _next(listener)
        assert _decode(analysis_transport, wake_delivery).task_id == wake.task_id
        await wake_delivery.ack()

        claimed_despite_foreign_lock = await _next(listener)
        await claimed_despite_foreign_lock.ack()

    assert (
        await analysis_transport.redis.ttl(lock_key),
        await analysis_transport.redis.get(lock_key),
        await _pending_count(analysis_transport, "curation"),
    ) == (-1, foreign_token, 0)


async def test_group_recreation_at_zero_replays_acked_retained_messages(
    analysis_transport: AnalysisTransport,
) -> None:
    """ACK済みでもgroup loss後は再配達され、rate-limit returnを完了保証にできない。

    task側の「gate拒否ならService/downstreamなしで正常return」は
    analysis/{curation,assessment}/test_tasks.py のquota skip testが固定する。
    ここではその正常ACK後もretained payloadが再進入し得るtransport側だけを固定する。
    """
    expected = {
        (await _enqueue(analysis_transport, stage)).task_id
        for stage in ("curation", "assessment")
    }
    await analysis_transport.broker.startup()

    async with _listener(analysis_transport) as listener:
        for _ in range(2):
            delivery = await _next(listener)
            await delivery.ack()

    for stream in (
        analysis_transport.curation_stream,
        analysis_transport.assessment_stream,
    ):
        await analysis_transport.redis.xgroup_destroy(stream, analysis_transport.group)
    await analysis_transport.broker._declare_consumer_group()

    replayed: set[str] = set()
    async with _listener(analysis_transport) as listener:
        for _ in range(2):
            delivery = await _next(listener)
            replayed.add(_decode(analysis_transport, delivery).task_id)
            await delivery.ack()

    assert (
        replayed,
        await analysis_transport.redis.xlen(analysis_transport.curation_stream),
        await analysis_transport.redis.xlen(analysis_transport.assessment_stream),
        await _pending_count(analysis_transport, "curation"),
        await _pending_count(analysis_transport, "assessment"),
    ) == (expected, 1, 1, 0, 0)
