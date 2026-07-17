"""実Redisでのpipeline Stream health snapshot契約。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from redis import asyncio as aioredis
from redis.asyncio import Redis

from app.config import settings
from app.queue.stream_health import (
    StreamHealthError,
    StreamHealthSnapshot,
    StreamHealthTarget,
    read_stream_health,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
    pytest.mark.xdist_group("redis"),
]


@pytest.fixture
async def stream_case() -> AsyncIterator[tuple[Redis, StreamHealthTarget]]:
    """各case専用Streamを作り、成否にかかわらず削除する。"""
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    target = StreamHealthTarget(
        stage="curation",
        stream=f"test:pipeline:curation:health:{uuid4().hex}",
        group="taskiq",
    )
    try:
        yield redis, target
    finally:
        await redis.delete(target.stream)
        await redis.aclose()


def _counts_and_age_presence(
    snapshot: StreamHealthSnapshot,
) -> tuple[int, int, int, bool, bool, bool]:
    return (
        snapshot.retained_entries,
        snapshot.lag,
        snapshot.pending,
        snapshot.oldest_undelivered_enqueue_age is not None,
        snapshot.oldest_pending_enqueue_age is not None,
        snapshot.oldest_outstanding_enqueue_age is not None,
    )


async def test_real_redis_empty_lag_pending_and_mixed_states(
    stream_case: tuple[Redis, StreamHealthTarget],
) -> None:
    """groupの配達状態遷移をretained / lag / pendingへ正しく分類する。"""
    redis, target = stream_case
    await redis.xgroup_create(
        target.stream,
        target.group,
        id="0-0",
        mkstream=True,
    )
    empty = await read_stream_health(redis, target)

    await redis.xadd(target.stream, {"payload": "first"})
    lag_only = await read_stream_health(redis, target)

    await redis.xreadgroup(
        target.group,
        "integration-consumer",
        {target.stream: ">"},
        count=1,
    )
    pending_only = await read_stream_health(redis, target)

    await redis.xadd(target.stream, {"payload": "second"})
    mixed = await read_stream_health(redis, target)

    assert tuple(
        _counts_and_age_presence(snapshot)
        for snapshot in (empty, lag_only, pending_only, mixed)
    ) == (
        (0, 0, 0, False, False, False),
        (1, 1, 0, True, False, True),
        (1, 0, 1, False, True, True),
        (2, 1, 1, True, True, True),
    )


async def test_real_redis_uses_first_live_after_delivery_and_min_pel_ids(
    stream_case: tuple[Redis, StreamHealthTarget],
) -> None:
    """undelivered / pending ageを各契約上の最小Stream IDから算出する。"""
    redis, target = stream_case
    seconds, microseconds = await redis.time()
    now_ms = seconds * 1_000 + microseconds // 1_000
    pending_id = f"{now_ms - 30_000}-0"
    undelivered_id = f"{now_ms - 10_000}-0"
    await redis.xadd(target.stream, {"payload": "pending"}, id=pending_id)
    await redis.xadd(target.stream, {"payload": "undelivered"}, id=undelivered_id)
    await redis.xgroup_create(target.stream, target.group, id="0-0")
    await redis.xreadgroup(
        target.group,
        "integration-consumer",
        {target.stream: ">"},
        count=1,
    )

    snapshot = await read_stream_health(redis, target)
    expected_pending_age = snapshot.observation_timestamp - (now_ms - 30_000) / 1_000
    expected_undelivered_age = (
        snapshot.observation_timestamp - (now_ms - 10_000) / 1_000
    )

    assert (
        (snapshot.retained_entries, snapshot.lag, snapshot.pending),
        snapshot.oldest_undelivered_enqueue_age
        == pytest.approx(expected_undelivered_age, abs=0.002),
        snapshot.oldest_pending_enqueue_age
        == pytest.approx(expected_pending_age, abs=0.002),
        snapshot.oldest_outstanding_enqueue_age
        == pytest.approx(expected_pending_age, abs=0.002),
    ) == ((2, 1, 1), True, True, True)


async def test_real_redis_ghost_pel_keeps_pending_count_and_enqueue_age(
    stream_case: tuple[Redis, StreamHealthTarget],
) -> None:
    """XDEL後もPEL最小IDをpendingとして扱い、live未配達へ誤分類しない。"""
    redis, target = stream_case
    seconds, microseconds = await redis.time()
    now_ms = seconds * 1_000 + microseconds // 1_000
    pending_id = f"{now_ms - 20_000}-0"
    await redis.xadd(target.stream, {"payload": "pending"}, id=pending_id)
    await redis.xgroup_create(target.stream, target.group, id="0-0")
    await redis.xreadgroup(
        target.group,
        "integration-consumer",
        {target.stream: ">"},
        count=1,
    )
    await redis.xdel(target.stream, pending_id)

    snapshot = await read_stream_health(redis, target)
    expected_pending_age = snapshot.observation_timestamp - (now_ms - 20_000) / 1_000

    assert (
        snapshot.retained_entries,
        snapshot.lag,
        snapshot.pending,
        snapshot.oldest_undelivered_enqueue_age,
        snapshot.oldest_pending_enqueue_age
        == pytest.approx(expected_pending_age, abs=0.002),
        snapshot.oldest_outstanding_enqueue_age
        == pytest.approx(expected_pending_age, abs=0.002),
    ) == (0, 0, 1, None, True, True)


@pytest.mark.parametrize(
    ("missing_kind", "expected_reason"),
    [("stream", "stream_missing"), ("group", "group_missing")],
)
async def test_real_redis_missing_stream_or_group_is_not_zero_snapshot(
    stream_case: tuple[Redis, StreamHealthTarget],
    missing_kind: str,
    expected_reason: str,
) -> None:
    """欠落状態はcount=0の正常snapshotではなく固定reasonの観測失敗にする。"""
    redis, target = stream_case
    if missing_kind == "group":
        await redis.xadd(target.stream, {"payload": "group-not-created"})

    with pytest.raises(StreamHealthError) as raised:
        await read_stream_health(redis, target)

    assert (raised.value.stage, raised.value.reason) == (
        "curation",
        expected_reason,
    )
