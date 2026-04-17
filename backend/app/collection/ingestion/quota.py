"""ソースごとの日次クォータ管理 — Redis INCR + EXPIRE。

Fetcher が ClassVar で宣言した DAILY_REQUEST_LIMIT を Task 層が enforce する。
Redis 障害時は fail-open（フェッチを許可）。
"""

from __future__ import annotations

from datetime import date

import structlog

from app.redis import get_redis

logger = structlog.get_logger(__name__)

_KEY_PREFIX = "source_quota"
_TTL_SECONDS = 25 * 3600  # 25 時間 — 日付境界を十分に超える


async def check_daily_quota(source_id: int, limit: int) -> bool:
    """日次クォータに余裕があれば True を返す。

    呼び出しごとにカウンタを +1 する（INCR）。
    count <= limit なら許可、超過なら拒否。
    Redis 障害時は True（fail-open）。
    """
    key = f"{_KEY_PREFIX}:{source_id}:{date.today().isoformat()}"
    try:
        client = get_redis()
        count = await client.incr(key)
        await client.expire(key, _TTL_SECONDS)
        return count <= limit
    except Exception:
        logger.warning(
            "redis_check_daily_quota_failed",
            source_id=source_id,
            exc_info=True,
        )
        return True
