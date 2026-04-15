"""Async Redis client — lazy singleton connection pool.

Symmetric with ``app.db`` for PostgreSQL: infrastructure connection config
lives at the ``app/`` top level, while domain-specific caches and limiters
live in their respective domain packages.
"""

from __future__ import annotations

import redis.asyncio as aioredis

from app.config import settings

_pool: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    """Return a shared async Redis client (created on first call)."""
    global _pool  # noqa: PLW0603
    if _pool is None:
        _pool = aioredis.from_url(
            settings.redis_url,
            decode_responses=True,
        )
    return _pool
