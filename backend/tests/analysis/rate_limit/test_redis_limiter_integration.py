"""実 Redis での SlidingWindowLimiter (Lua スクリプト) 契約。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from redis import asyncio as aioredis
from redis.asyncio import Redis

from app.config import settings
from app.redis.sliding_window import RateLimitExceededError, SlidingWindowLimiter

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
    pytest.mark.xdist_group("redis"),
]


class _StopSpin(Exception):
    """sleep への到達を捕捉しループを脱出させるテスト専用 sentinel。"""


@pytest.fixture
async def limiter_case() -> AsyncIterator[tuple[Redis, str]]:
    """実Redisへ接続し、リミッターの書き込み先となる一意なkeyをテストへ渡す。"""
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    key = f"test:ratelimit:{uuid4().hex}"
    try:
        yield redis, key
    finally:
        await redis.delete(key)
        await redis.aclose()


async def test_real_redis_acquire_records_each_request_in_zset(
    limiter_case: tuple[Redis, str],
) -> None:
    """許可範囲内の acquire は Lua の ZADD で ZSET に 1 件ずつ記録される。"""
    redis, key = limiter_case
    limiter = SlidingWindowLimiter(
        redis, key, max_requests=3, window_seconds=60, block=False
    )

    for _ in range(3):
        await limiter.acquire()

    assert await redis.zcard(key) == 3


async def test_real_redis_rejects_when_at_capacity(
    limiter_case: tuple[Redis, str],
) -> None:
    """count == max_requests での 4 回目は block=False だと RateLimitExceededError。

    Lua の `count < max_requests` 判定の off-by-one を直接捕捉する正本テスト。
    """
    redis, key = limiter_case
    limiter = SlidingWindowLimiter(
        redis, key, max_requests=3, window_seconds=60, block=False
    )
    for _ in range(3):
        await limiter.acquire()

    with pytest.raises(RateLimitExceededError):
        await limiter.acquire()


async def test_real_redis_recovers_after_window_elapses(
    limiter_case: tuple[Redis, str],
) -> None:
    """ZREMRANGEBYSCORE が window 経過後のエントリを実際に失効させ、枠が回復する。"""
    redis, key = limiter_case
    limiter = SlidingWindowLimiter(
        redis, key, max_requests=1, window_seconds=1, block=False
    )

    await limiter.acquire()
    with pytest.raises(RateLimitExceededError):
        await limiter.acquire()

    await asyncio.sleep(1.2)
    await limiter.acquire()  # window 経過により例外なく成功するはず


async def test_real_redis_blocking_wait_derives_from_oldest_entry_score(
    limiter_case: tuple[Redis, str],
) -> None:
    """block=True の wait は Lua が返す oldest_score 由来で、fallback (1.0s) ではない。

    oldest_score が (Lua の退行等で) 0 になると production は fallback の
    1.0 秒でスピンする。ここでは満杯直後の wait が window_seconds 相当である
    ことを固定し、fallback との取り違えを検出する。
    """
    redis, key = limiter_case
    window_seconds = 60
    limiter = SlidingWindowLimiter(
        redis, key, max_requests=1, window_seconds=window_seconds
    )
    await limiter.acquire()

    captured_wait: list[float] = []

    def _capture_and_stop(wait: float) -> None:
        captured_wait.append(wait)
        raise _StopSpin

    with patch(
        "app.redis.sliding_window.asyncio.sleep",
        AsyncMock(side_effect=_capture_and_stop),
    ):
        with pytest.raises(_StopSpin):
            await limiter.acquire()

    assert len(captured_wait) == 1
    wait = captured_wait[0]
    assert window_seconds - 10 < wait <= window_seconds


async def test_real_redis_concurrent_acquire_never_exceeds_max_requests(
    limiter_case: tuple[Redis, str],
) -> None:
    """並行 6 本の acquire でも Lua の check-and-add はアトミックで上限を厳守する。"""
    redis, key = limiter_case
    limiter = SlidingWindowLimiter(
        redis, key, max_requests=5, window_seconds=60, block=False
    )

    results = await asyncio.gather(
        *(limiter.acquire() for _ in range(6)), return_exceptions=True
    )

    successes = [result for result in results if result is None]
    failures = [
        result for result in results if isinstance(result, RateLimitExceededError)
    ]
    # 想定外の例外が紛れて計上漏れになっていないこと
    assert len(successes) + len(failures) == len(results)
    assert (len(successes), len(failures)) == (5, 1)


async def test_real_redis_acquire_sets_ttl_bounded_by_window_and_margin(
    limiter_case: tuple[Redis, str],
) -> None:
    """key の TTL は window_seconds 以上 (+60 の実装上の安全マージン以内) に設定される。

    下限が window より短いと ZSET が window 途中で消えカウンタがリセットされ、
    レート制限を突破し得るため危険な退行方向として明示的に固定する。
    """
    redis, key = limiter_case
    window_seconds = 60
    limiter = SlidingWindowLimiter(
        redis, key, max_requests=10, window_seconds=window_seconds, block=False
    )

    await limiter.acquire()

    ttl = await redis.ttl(key)
    # +60 は実装 (SlidingWindowLimiter.acquire) が積む安全マージン
    assert window_seconds <= ttl <= window_seconds + 60
