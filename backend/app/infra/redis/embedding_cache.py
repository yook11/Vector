"""Async Redis cache for search-query embeddings.

Caches embedding vectors keyed by SHA256 of the query text so repeated searches
skip the Gemini API call. Callers are expected to pass already-normalized text
(see ``ArticleListParams._normalize_q``) so trivial variants collapse to the
same key. Fire-and-forget safe: on Redis outage, ``get`` returns None and
``set`` silently no-ops so the caller falls back to a fresh embedding.

NOTE: ``_get_client`` is borrowed from ``redis_cache`` to share the lazy
singleton connection pool. A follow-up refactor will split the shared Redis
client into its own module.
"""

from __future__ import annotations

import hashlib
import json

import structlog

from app.infra.redis.cache import _get_client

logger = structlog.get_logger(__name__)

_KEY_PREFIX = "embed:query"
_TTL_SECONDS = 7 * 24 * 3600  # 7 days — embeddings for a given input are deterministic


def _cache_key(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"{_KEY_PREFIX}:{digest}"


async def get_query_embedding(text: str) -> list[float] | None:
    """Return the cached embedding vector for *text*, or None on miss/error."""
    try:
        client = _get_client()
        raw = await client.get(_cache_key(text))
        if raw is None:
            return None
        return json.loads(raw)
    except Exception:
        logger.warning("embedding_cache_get_failed", exc_info=True)
        return None


async def set_query_embedding(text: str, vector: list[float]) -> None:
    """Persist *vector* for *text* with a TTL."""
    try:
        client = _get_client()
        await client.set(
            _cache_key(text),
            json.dumps(vector),
            ex=_TTL_SECONDS,
        )
    except Exception:
        logger.warning("embedding_cache_set_failed", exc_info=True)
