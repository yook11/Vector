"""Stage 5 embedding の provider hold(一時停止フラグ)。"""

from __future__ import annotations

import logfire
import structlog
from redis.asyncio import Redis

logger = structlog.get_logger(__name__)

_HOLD_KEY = "embedding:hold"
_HOLD_TTL_SECONDS = 6 * 60 * 60  # 6h

_hold_set_counter = logfire.metric_counter(
    "vector.embedding.hold_set",
    unit="1",
    description="Embedding hold が set された回数 (stage-wide terminal failure)",
)
_hold_set_failed_counter = logfire.metric_counter(
    "vector.embedding.hold_set_failed",
    unit="1",
    description="Embedding hold の set が Redis 障害等で失敗した回数",
)


async def set_embedding_hold(redis: Redis, *, reason: str) -> None:
    """stage-wide terminal failure 検出時に hold を TTL 付きで立てる。"""
    try:
        await redis.set(_HOLD_KEY, reason, ex=_HOLD_TTL_SECONDS)
    except Exception:  # noqa: BLE001 — hold は best-effort
        _hold_set_failed_counter.add(1, attributes={"reason": reason})
        logger.warning("embedding_hold_set_failed", reason=reason, exc_info=True)
        return
    _hold_set_counter.add(1, attributes={"reason": reason})


async def is_embedding_held(redis: Redis) -> bool:
    """hold が立っているかを返す。Redis 障害時は fail-open。"""
    try:
        return bool(await redis.exists(_HOLD_KEY))
    except Exception:  # noqa: BLE001 — Redis 障害は救済を止めない
        logger.warning("embedding_hold_check_failed", exc_info=True)
        return False
