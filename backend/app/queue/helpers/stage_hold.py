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

_REFRESH_RECOVERY_HOLD_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("EXPIRE", KEYS[1], ARGV[2])
end
return 0
"""
_RELEASE_RECOVERY_HOLD_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
end
return 0
"""

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

_RECOVERY_HOLD_SPECS = {
    _CURATION_SPEC.name: _CURATION_SPEC,
    _ASSESSMENT_SPEC.name: _ASSESSMENT_SPEC,
}

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


def _recovery_hold_spec(stage: str) -> _StageHoldSpec:
    """recovery ownershipを許可するanalysis stageのhold仕様を返す。"""
    try:
        return _RECOVERY_HOLD_SPECS[stage]
    except KeyError:
        raise ValueError(f"Unsupported recovery hold stage: {stage}") from None


async def acquire_recovery_hold(redis: Redis, *, stage: str, token: str) -> bool:
    """既存holdを上書きせずrecovery tokenの所有権を取得する。"""
    spec = _recovery_hold_spec(stage)
    return bool(
        await redis.set(
            spec.key,
            token,
            nx=True,
            ex=_HOLD_TTL_SECONDS,
        )
    )


async def refresh_recovery_hold(redis: Redis, *, stage: str, token: str) -> bool:
    """所有tokenが一致するrecovery holdだけをatomicに延長する。"""
    spec = _recovery_hold_spec(stage)
    return bool(
        await redis.eval(
            _REFRESH_RECOVERY_HOLD_SCRIPT,
            1,
            spec.key,
            token,
            _HOLD_TTL_SECONDS,
        )
    )


async def release_recovery_hold(redis: Redis, *, stage: str, token: str) -> bool:
    """所有tokenが一致するrecovery holdだけをatomicに削除する。"""
    spec = _recovery_hold_spec(stage)
    return bool(
        await redis.eval(
            _RELEASE_RECOVERY_HOLD_SCRIPT,
            1,
            spec.key,
            token,
        )
    )


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
