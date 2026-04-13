"""Redis ZSET sliding-window rate limiter.

Each ``acquire()`` call atomically checks capacity and records the
request via a Lua script.  Expired entries (older than *window_seconds*)
are pruned in the same script.

Timing uses Redis server time (``redis.call('TIME')``) as the single
source of truth so that clock skew across distributed workers does not
affect correctness.

If the set already has *max_requests* members the limiter either sleeps
until the oldest entry expires (``block=True`` — RPM) or raises
``RateLimitExceededError`` immediately (``block=False`` — RPD).

Members are UUIDs (guaranteed unique); scores are Redis server
timestamps.  The Lua script ensures check-and-add is atomic.
"""

from __future__ import annotations

import asyncio
import uuid

import redis.asyncio as aioredis
import structlog

logger = structlog.get_logger(__name__)


class RateLimitExceededError(Exception):
    """Rate limit exceeded and blocking is disabled."""


# Lua script: get server time -> prune -> count -> conditionally add.
# Returns: [1, 0, now_str] on success,
#          [0, oldest_score_str, now_str] when at capacity.
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


class RateLimiter:
    """Distributed sliding-window rate limiter backed by Redis ZSET."""

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
        """Acquire a rate-limit slot.

        - ``block=True``: sleep until a slot frees up, then proceed.
        - ``block=False``: raise ``RateLimitExceededError`` if full.
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

            # At capacity
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
