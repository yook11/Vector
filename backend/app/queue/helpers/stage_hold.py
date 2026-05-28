"""backfill stage hold の Redis gate。

provider / stage 全体の健全性問題を観測した task が TTL 付き hold を立て、
backfill cron が同じ key を読んで再投入を一時停止する。これは task
orchestration の運用制御であり、domain state ではない。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import logfire
import structlog
from redis.asyncio import Redis

logger = structlog.get_logger(__name__)

_HOLD_TTL_SECONDS = 6 * 60 * 60  # 6h

StageHoldName = Literal["curation", "assessment", "embedding"]


@dataclass(frozen=True, slots=True)
class _StageHoldSpec:
    name: StageHoldName
    key: str
    set_metric_name: str
    set_failed_metric_name: str
    log_prefix: str


_CURATION_SPEC = _StageHoldSpec(
    name="curation",
    key="curation:hold",
    set_metric_name="vector.curation.hold_set",
    set_failed_metric_name="vector.curation.hold_set_failed",
    log_prefix="curation",
)
_ASSESSMENT_SPEC = _StageHoldSpec(
    name="assessment",
    key="assessment:hold",
    set_metric_name="vector.assessment.hold_set",
    set_failed_metric_name="vector.assessment.hold_set_failed",
    log_prefix="assessment",
)
_EMBEDDING_SPEC = _StageHoldSpec(
    name="embedding",
    key="embedding:hold",
    set_metric_name="vector.embedding.hold_set",
    set_failed_metric_name="vector.embedding.hold_set_failed",
    log_prefix="embedding",
)

_HOLD_SET_COUNTERS = {
    spec.name: logfire.metric_counter(
        spec.set_metric_name,
        unit="1",
        description=f"{spec.name.title()} hold が set された回数",
    )
    for spec in (_CURATION_SPEC, _ASSESSMENT_SPEC, _EMBEDDING_SPEC)
}
_HOLD_SET_FAILED_COUNTERS = {
    spec.name: logfire.metric_counter(
        spec.set_failed_metric_name,
        unit="1",
        description=f"{spec.name.title()} hold の set が Redis 障害等で失敗した回数",
    )
    for spec in (_CURATION_SPEC, _ASSESSMENT_SPEC, _EMBEDDING_SPEC)
}


async def _set_stage_hold(redis: Redis, spec: _StageHoldSpec, *, reason: str) -> None:
    """stage-wide failure 検出時に hold を TTL 付きで立てる。"""
    try:
        await redis.set(spec.key, reason, ex=_HOLD_TTL_SECONDS)
    except Exception:  # noqa: BLE001 — hold は best-effort
        _HOLD_SET_FAILED_COUNTERS[spec.name].add(1, attributes={"reason": reason})
        logger.warning(
            f"{spec.log_prefix}_hold_set_failed", reason=reason, exc_info=True
        )
        return
    _HOLD_SET_COUNTERS[spec.name].add(1, attributes={"reason": reason})


async def _is_stage_held(redis: Redis, spec: _StageHoldSpec) -> bool:
    """hold が立っているかを返す。Redis 障害時は fail-open。"""
    try:
        return bool(await redis.exists(spec.key))
    except Exception:  # noqa: BLE001 — Redis 障害は救済を止めない
        logger.warning(f"{spec.log_prefix}_hold_check_failed", exc_info=True)
        return False


async def set_curation_hold(redis: Redis, *, reason: str) -> None:
    """Stage 3 curation の hold を TTL 付きで立てる。"""
    await _set_stage_hold(redis, _CURATION_SPEC, reason=reason)


async def is_curation_held(redis: Redis) -> bool:
    """Stage 3 curation の hold 状態を返す。"""
    return await _is_stage_held(redis, _CURATION_SPEC)


async def set_assessment_hold(redis: Redis, *, reason: str) -> None:
    """Stage 4 assessment の hold を TTL 付きで立てる。"""
    await _set_stage_hold(redis, _ASSESSMENT_SPEC, reason=reason)


async def is_assessment_held(redis: Redis) -> bool:
    """Stage 4 assessment の hold 状態を返す。"""
    return await _is_stage_held(redis, _ASSESSMENT_SPEC)


async def set_embedding_hold(redis: Redis, *, reason: str) -> None:
    """Stage 5 embedding の hold を TTL 付きで立てる。"""
    await _set_stage_hold(redis, _EMBEDDING_SPEC, reason=reason)


async def is_embedding_held(redis: Redis) -> bool:
    """Stage 5 embedding の hold 状態を返す。"""
    return await _is_stage_held(redis, _EMBEDDING_SPEC)
