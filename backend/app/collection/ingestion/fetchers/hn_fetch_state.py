"""Hacker News 固有の増分取得 state — Redis に最終フェッチ時刻を保存する。

HN の Algolia Search API は ``created_at_i>`` フィルタで増分取得を実現するため、
fetcher 単位で「前回フェッチ時刻」を持つ必要がある。これは HN 固有の概念であり
(RSS は ETag/Last-Modified で代替)、``http_cache.py`` と対称に Redis に閉じる。

Redis 障害時は ``None`` にフォールバックし、フルスキャン相当で動作する
(データ欠損は発生しない)。
"""

from __future__ import annotations

from datetime import datetime

import structlog

from app.redis import get_redis

logger = structlog.get_logger(__name__)

_KEY_PREFIX = "hn_fetch_state"


def _state_key(source_id: int) -> str:
    return f"{_KEY_PREFIX}:{source_id}"


async def get_last_fetched_at(source_id: int) -> datetime | None:
    """``source_id`` の最終フェッチ時刻を返す。未設定時は ``None``。"""
    try:
        client = get_redis()
        raw = await client.get(_state_key(source_id))
        if raw is None:
            return None
        return datetime.fromisoformat(raw)
    except Exception:
        logger.warning(
            "redis_get_hn_fetch_state_failed", source_id=source_id, exc_info=True
        )
        return None


async def set_last_fetched_at(source_id: int, ts: datetime) -> None:
    """``source_id`` の最終フェッチ時刻を ISO 文字列で保存する (TTL なし)。"""
    try:
        client = get_redis()
        await client.set(_state_key(source_id), ts.isoformat())
    except Exception:
        logger.warning(
            "redis_set_hn_fetch_state_failed", source_id=source_id, exc_info=True
        )
