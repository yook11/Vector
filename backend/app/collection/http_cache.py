"""HTTP 条件付き GET キャッシュ（etag / last-modified）用の非同期 Redis ヘルパー。

キーはニュースソース単位でスコープされ、7 日で失効する
（通常の 12 時間サイクルを十分に超えるフェッチ間隔をカバーする）。
いずれの関数も fire-and-forget で安全に扱え、Redis 障害時は次回フェッチで
フルダウンロードに縮退するだけでデータ欠損は発生しない。
"""

from __future__ import annotations

import json

import structlog

from app.redis import get_redis

logger = structlog.get_logger(__name__)

_KEY_PREFIX = "source"
_TTL_SECONDS = 7 * 24 * 3600  # 7 日


def _cache_key(source_id: int) -> str:
    return f"{_KEY_PREFIX}:{source_id}:http_cache"


async def get_http_cache(source_id: int) -> tuple[str | None, str | None]:
    """``source_id`` の ``(etag, last_modified)`` を返す。

    未登録時は ``(None, None)`` を返す。
    """
    try:
        client = get_redis()
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
    """``source_id`` に対して ``etag`` と ``last_modified`` を 7 日 TTL で保存する。"""
    if etag is None and last_modified is None:
        return
    try:
        client = get_redis()
        payload = json.dumps(
            {"etag": etag, "last_modified": last_modified},
        )
        await client.set(_cache_key(source_id), payload, ex=_TTL_SECONDS)
    except Exception:
        logger.warning(
            "redis_set_http_cache_failed", source_id=source_id, exc_info=True
        )
