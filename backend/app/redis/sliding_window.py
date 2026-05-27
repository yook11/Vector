"""Redis ZSET スライディングウィンドウ primitive。

``acquire()`` は Lua スクリプトを介して容量チェックとリクエスト記録を
アトミックに実行する。*window_seconds* より古いエントリは同じスクリプト内で削除する。

時刻には Redis サーバー時刻（``redis.call('TIME')``）を
Single Source of Truth として用いる。
分散ワーカー間でクロックがずれていても正しく動くようにするため。

ZSET のメンバ数が *max_requests* に達している場合、``block=True`` であれば
最古のエントリが期限切れになるまでスリープし、
``block=False`` であれば ``RateLimitExceededError`` を即座に送出する。

メンバには UUID を用いて一意性を保証し、スコアには Redis サーバー時刻を用いる。
Lua スクリプトによって check-and-add はアトミックに保たれる。
"""

from __future__ import annotations

import asyncio
import uuid

import redis.asyncio as aioredis
import structlog

logger = structlog.get_logger(__name__)


class RateLimitExceededError(Exception):
    """レート制限超過かつ block 無効時に送出される例外。"""


# Lua スクリプト: サーバー時刻取得 -> 期限切れ削除 -> カウント -> 条件付き追加。
# 戻り値: 成功時は [1, 0, now_str]、
#         満杯時は [0, oldest_score_str, now_str]。
_ACQUIRE_SCRIPT = """
local key = KEYS[1]
local max_requests = tonumber(ARGV[1])
local window_seconds = tonumber(ARGV[2])
local member = ARGV[3]
local ttl = tonumber(ARGV[4])

local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local window_start = now - window_seconds

redis.call('ZREMRANGEBYSCORE', key, '-inf', window_start)

local count = redis.call('ZCARD', key)
if count < max_requests then
    redis.call('ZADD', key, now, member)
    redis.call('EXPIRE', key, ttl)
    return {1, 0, tostring(now)}
end

local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
local oldest_score = 0
if #oldest >= 2 then
    oldest_score = oldest[2]
end
return {0, oldest_score, tostring(now)}
"""


class SlidingWindowLimiter:
    """Redis ZSET による分散スライディングウィンドウ・レートリミッター。"""

    def __init__(
        self,
        redis: aioredis.Redis,
        key: str,
        max_requests: int,
        window_seconds: int,
        *,
        block: bool = True,
    ) -> None:
        self._redis = redis
        self._key = key
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._block = block
        self._script = redis.register_script(_ACQUIRE_SCRIPT)

    async def acquire(self) -> None:
        """レート制限のスロットを 1 つ取得する。

        - ``block=True``: スロットが空くまでスリープしてから処理を続行する。
        - ``block=False``: 満杯なら ``RateLimitExceededError`` を送出する。
        """
        while True:
            member = uuid.uuid4().hex
            ttl = self._window_seconds + 60

            result = await self._script(
                keys=[self._key],
                args=[self._max_requests, self._window_seconds, member, ttl],
            )
            acquired = int(result[0])
            server_now = float(result[2])

            if acquired:
                return

            # 満杯
            if not self._block:
                raise RateLimitExceededError(
                    f"Rate limit exceeded: {self._max_requests} "
                    f"requests per {self._window_seconds}s"
                )

            oldest_score = float(result[1])
            if oldest_score > 0:
                wait = (oldest_score + self._window_seconds) - server_now
            else:
                wait = 1.0

            wait = max(wait, 0.1)
            logger.info(
                "rate_limiter_waiting",
                key=self._key,
                current=self._max_requests,
                max=self._max_requests,
                wait_seconds=round(wait, 2),
            )
            await asyncio.sleep(wait)
