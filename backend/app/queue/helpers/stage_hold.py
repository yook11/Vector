"""backfill stage hold の Redis gate。

provider / stage 全体の健全性問題を観測した task が TTL 付き hold を立て、
backfill cron が同じ key を読んで再投入を一時停止する。これは task
orchestration の運用制御であり、domain state ではない。
"""

from __future__ import annotations

from typing import Literal, get_args

import logfire
import structlog
from redis.asyncio import Redis

logger = structlog.get_logger(__name__)

_HOLD_TTL_SECONDS = 6 * 60 * 60  # 6h

StageHoldName = Literal["curation", "assessment", "embedding"]
_STAGE_HOLD_NAMES: tuple[StageHoldName, ...] = get_args(StageHoldName)


def _hold_key(name: StageHoldName) -> str:
    """stage 共通の hold key ``{stage}:hold`` を返す。"""
    return f"{name}:hold"


_HOLD_SET_COUNTERS = {
    name: logfire.metric_counter(
        f"vector.{name}.hold_set",
        unit="1",
        description=f"{name.title()} hold が set された回数",
    )
    for name in _STAGE_HOLD_NAMES
}
_HOLD_SET_FAILED_COUNTERS = {
    name: logfire.metric_counter(
        f"vector.{name}.hold_set_failed",
        unit="1",
        description=f"{name.title()} hold の set が Redis 障害等で失敗した回数",
    )
    for name in _STAGE_HOLD_NAMES
}


async def _set_stage_hold(redis: Redis, name: StageHoldName, *, reason: str) -> None:
    """stage-wide failure 検出時に hold を TTL 付きで立てる。"""
    try:
        await redis.set(_hold_key(name), reason, ex=_HOLD_TTL_SECONDS)
    except Exception:  # noqa: BLE001 — hold は best-effort
        _HOLD_SET_FAILED_COUNTERS[name].add(1, attributes={"reason": reason})
        logger.warning(f"{name}_hold_set_failed", reason=reason, exc_info=True)
        return
    _HOLD_SET_COUNTERS[name].add(1, attributes={"reason": reason})


async def _is_stage_held(redis: Redis, name: StageHoldName) -> bool:
    """hold が立っているかを返す。Redis 障害時は fail-open。"""
    try:
        return bool(await redis.exists(_hold_key(name)))
    except Exception:  # noqa: BLE001 — Redis 障害は救済を止めない
        logger.warning(f"{name}_hold_check_failed", exc_info=True)
        return False


async def set_curation_hold(redis: Redis, *, reason: str) -> None:
    """Stage 3 curation の hold を TTL 付きで立てる。"""
    await _set_stage_hold(redis, "curation", reason=reason)


async def is_curation_held(redis: Redis) -> bool:
    """Stage 3 curation の hold 状態を返す。"""
    return await _is_stage_held(redis, "curation")


async def set_assessment_hold(redis: Redis, *, reason: str) -> None:
    """Stage 4 assessment の hold を TTL 付きで立てる。"""
    await _set_stage_hold(redis, "assessment", reason=reason)


async def is_assessment_held(redis: Redis) -> bool:
    """Stage 4 assessment の hold 状態を返す。"""
    return await _is_stage_held(redis, "assessment")


async def set_embedding_hold(redis: Redis, *, reason: str) -> None:
    """Stage 5 embedding の hold を TTL 付きで立てる。"""
    await _set_stage_hold(redis, "embedding", reason=reason)


async def is_embedding_held(redis: Redis) -> bool:
    """Stage 5 embedding の hold 状態を返す。"""
    return await _is_stage_held(redis, "embedding")
