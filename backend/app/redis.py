"""非同期 Redis クライアント — 遅延初期化するシングルトン接続プール。

PostgreSQL 用の ``app.db`` と対称。インフラ接続の設定は ``app/`` 直下に置き、
ドメイン固有のキャッシュやレートリミッターは各ドメインパッケージに置く。
"""

from __future__ import annotations

import redis.asyncio as aioredis

from app.config import settings

_pool: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    """共有の非同期 Redis クライアントを返す（初回呼び出し時に生成）。"""
    global _pool  # noqa: PLW0603
    if _pool is None:
        _pool = aioredis.from_url(
            settings.redis_url,
            decode_responses=True,
        )
    return _pool
