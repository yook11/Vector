"""Stage 3 curation の provider hold(一時停止フラグ)。

terminal_keep(key/残高/config 等、provider/stage 全体の健全性問題)発生時に TTL 付きで
立て、TTL の間は backfill 再投入を停止する。真実ではなく「今叩くな」のソフトフラグ —
消えても次 batch が少し失敗して再 set される。Redis 障害時は fail-open(救済を止めない)。
"""

from __future__ import annotations

import structlog
from redis.asyncio import Redis

logger = structlog.get_logger(__name__)

_HOLD_KEY = "curation:hold"
_HOLD_TTL_SECONDS = 6 * 60 * 60  # 6h


async def set_curation_hold(redis: Redis, *, reason: str) -> None:
    """terminal_keep 検出時に hold を TTL 付きで立てる(best-effort)。"""
    try:
        await redis.set(_HOLD_KEY, reason, ex=_HOLD_TTL_SECONDS)
    except Exception:  # noqa: BLE001 — hold は真実でない、set 失敗で業務を壊さない
        logger.warning("curation_hold_set_failed", reason=reason, exc_info=True)


async def is_curation_held(redis: Redis) -> bool:
    """hold が立っているかを返す。Redis 障害時は fail-open(救済を止めない)。"""
    try:
        return bool(await redis.exists(_HOLD_KEY))
    except Exception:  # noqa: BLE001 — Redis 障害は fail-open(救済を止めない)
        logger.warning("curation_hold_check_failed", exc_info=True)
        return False
