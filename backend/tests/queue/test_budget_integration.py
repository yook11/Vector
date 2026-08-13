"""実 Redis での consume_daily_budget (Lua スクリプト) 契約。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import uuid4

import pytest
from redis import asyncio as aioredis
from redis.asyncio import Redis

from app.config import settings
from app.queue.helpers.budget import _budget_key, consume_daily_budget
from app.shared.time import utc_now

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
    pytest.mark.xdist_group("redis"),
]


@pytest.fixture
async def budget_case() -> AsyncIterator[tuple[Redis, str]]:
    """test 専用 role を払い出し、日付跨ぎに備え setup/teardown 双方の key を消す。"""
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    role = f"test-{uuid4().hex}"
    setup_key = _budget_key(role)
    try:
        yield redis, role
    finally:
        await redis.delete(setup_key, _budget_key(role))
        await redis.aclose()


async def test_real_redis_consume_daily_budget_clamps_and_saturates_at_zero(
    budget_case: tuple[Redis, str],
) -> None:
    """daily_max 10 へ requested 6 を 3 回投入すると、
    granted は 6→4→0 と remaining へ clamp される。
    """
    redis, role = budget_case
    daily_max = 10
    requested = 6

    granted_sequence = [
        await consume_daily_budget(redis, role, requested, daily_max) for _ in range(3)
    ]

    # 1回目: 残り10に対しrequested6->6 / 2回目: 残り4に対しrequested6はclampされ4
    # / 3回目: 残り0で available<=0 のため即0
    assert granted_sequence == [6, 4, 0]


async def test_real_redis_consume_daily_budget_persists_cumulative_counter_at_max(
    budget_case: tuple[Redis, str],
) -> None:
    """枯渇まで消費した後、Redis 上の当日カウンタは daily_max ちょうどに一致する。"""
    redis, role = budget_case
    daily_max = 10

    await consume_daily_budget(redis, role, 6, daily_max)
    await consume_daily_budget(redis, role, 6, daily_max)
    await consume_daily_budget(redis, role, 6, daily_max)

    stored = await redis.get(_budget_key(role))
    assert int(stored) == daily_max


async def test_real_redis_consume_daily_budget_sets_ttl_within_26h_margin(
    budget_case: tuple[Redis, str],
) -> None:
    """消費 1 回後の key TTL は当日 UTC 末まで生存し、26h (実装定数) 以内に収まる。

    下限を仕様「当日 UTC 末まで生存する」から算出する。
    """
    redis, role = budget_case
    now = utc_now()
    next_utc_midnight = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    seconds_until_utc_midnight = (next_utc_midnight - now).total_seconds()

    await consume_daily_budget(redis, role, 1, 10)

    ttl = await redis.ttl(_budget_key(role))
    # 上限 26h は _BUDGET_TTL_SECONDS (実装定数): 日跨ぎの猶予を含む
    assert seconds_until_utc_midnight <= ttl <= 26 * 60 * 60


async def test_real_redis_concurrent_consume_daily_budget_never_exceeds_daily_max(
    budget_case: tuple[Redis, str],
) -> None:
    """並行 5 本の消費でも Lua の GET→INCRBY はアトミックで daily_max を超過しない。"""
    redis, role = budget_case
    daily_max = 10
    requested = 3

    results = await asyncio.gather(
        *(consume_daily_budget(redis, role, requested, daily_max) for _ in range(5))
    )

    # 総需要 5*3=15 > daily_max 10 のため、合計は必ず daily_max で頭打ちになる
    assert sum(results) == daily_max
