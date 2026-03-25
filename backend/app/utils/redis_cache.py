"""Thin async Redis helper for HTTP conditional-GET cache (etag / last-modified).

Keys are scoped per news-source and expire after 7 days (covers fetch intervals
well beyond the typical 12-hour cycle).  All functions are fire-and-forget safe:
a Redis outage degrades to a full download on the next fetch — no data loss.
"""

from __future__ import annotations

import json

import redis.asyncio as aioredis
import structlog

from app.config import settings

logger = structlog.get_logger(__name__)

_KEY_PREFIX = "source"
_TTL_SECONDS = 7 * 24 * 3600  # 7 days

# Lazy singleton — created on first call, reused across the process.
_pool: aioredis.Redis | None = None


def _get_client() -> aioredis.Redis:
    global _pool  # noqa: PLW0603
    if _pool is None:
        _pool = aioredis.from_url(
            settings.redis_url,
            decode_responses=True,
        )
    return _pool


def _cache_key(source_id: int) -> str:
    return f"{_KEY_PREFIX}:{source_id}:http_cache"


async def get_http_cache(source_id: int) -> tuple[str | None, str | None]:
    """Return ``(etag, last_modified)`` for *source_id*, or ``(None, None)``."""
    try:
        client = _get_client()
        raw = await client.get(_cache_key(source_id))
        if raw is None:
            return None, None
        data = json.loads(raw)
        return data.get("etag"), data.get("last_modified")
    except Exception:
        logger.warning(
            "redis_get_http_cache_failed", source_id=source_id, exc_info=True
        )
        return None, None


async def set_http_cache(
    source_id: int,
    etag: str | None,
    last_modified: str | None,
) -> None:
    """Persist *etag* and *last_modified* for *source_id* with a 7-day TTL."""
    if etag is None and last_modified is None:
        return
    try:
        client = _get_client()
        payload = json.dumps(
            {"etag": etag, "last_modified": last_modified},
        )
        await client.set(_cache_key(source_id), payload, ex=_TTL_SECONDS)
    except Exception:
        logger.warning(
            "redis_set_http_cache_failed", source_id=source_id, exc_info=True
        )
