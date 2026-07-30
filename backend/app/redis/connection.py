"""非同期 Redis クライアント — 遅延初期化するシングルトン接続プール。

PostgreSQL 用の ``app.db`` と対称。インフラ接続の設定は ``app.redis`` 配下に置き、
ドメイン固有のキャッシュや制御方針は各ドメインパッケージに置く。
"""

from __future__ import annotations

import redis.asyncio as aioredis

from app.config import settings
from app.redis.iam_auth import redis_connection_options

_pool: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    """共有の非同期 Redis クライアントを返す（初回呼び出し時に生成）。"""
    global _pool  # noqa: PLW0603
    if _pool is None:
        url, iam_kwargs = redis_connection_options(settings.redis_url)
        _pool = aioredis.from_url(
            url,
            decode_responses=True,
            **iam_kwargs,
        )
    return _pool
