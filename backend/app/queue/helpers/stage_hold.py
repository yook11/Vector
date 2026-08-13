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

from app.audit.domain.event import Stage

logger = structlog.get_logger(__name__)

_HOLD_TTL_SECONDS = 6 * 60 * 60  # 6h

# hold を持つのは AI provider 障害で止まりうる 3 stage だけ。
# 値の出所は Stage に一本化する。
HoldableStage = Literal[Stage.CURATION, Stage.ASSESSMENT, Stage.EMBEDDING]
_HOLDABLE_STAGES: tuple[HoldableStage, ...] = get_args(HoldableStage)


def _hold_key(stage: HoldableStage) -> str:
    """hold key ``{stage}:hold`` を返す。

    key pattern は infra/aws/valkey.tf の ACL 許可リストと 1:1。Stage の値を変えると
    key 名が変わるため、ACL と稼働中の hold の両方に波及する。
    """
    return f"{stage.value}:hold"


_HOLD_SET_COUNTERS = {
    stage: logfire.metric_counter(
        f"vector.{stage.value}.hold_set",
        unit="1",
        description=f"{stage.value.title()} hold が set された回数",
    )
    for stage in _HOLDABLE_STAGES
}
_HOLD_SET_FAILED_COUNTERS = {
    stage: logfire.metric_counter(
        f"vector.{stage.value}.hold_set_failed",
        unit="1",
        description=f"{stage.value.title()} hold の set が Redis 障害等で失敗した回数",
    )
    for stage in _HOLDABLE_STAGES
}


async def set_stage_hold(redis: Redis, stage: HoldableStage, *, reason: str) -> None:
    """stage-wide failure 検出時に hold を TTL 付きで立てる。"""
    try:
        await redis.set(_hold_key(stage), reason, ex=_HOLD_TTL_SECONDS)
    except Exception:  # noqa: BLE001 — hold は best-effort
        _HOLD_SET_FAILED_COUNTERS[stage].add(1, attributes={"reason": reason})
        logger.warning(f"{stage.value}_hold_set_failed", reason=reason, exc_info=True)
        return
    _HOLD_SET_COUNTERS[stage].add(1, attributes={"reason": reason})


async def is_stage_held(redis: Redis, stage: HoldableStage) -> bool:
    """hold が立っているかを返す。Redis 障害時は fail-open。"""
    try:
        return bool(await redis.exists(_hold_key(stage)))
    except Exception:  # noqa: BLE001 — Redis 障害は救済を止めない
        logger.warning(f"{stage.value}_hold_check_failed", exc_info=True)
        return False
