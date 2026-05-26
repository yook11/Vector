"""Stage 3 curation の provider hold(一時停止フラグ)。

terminal_keep(key/残高/config 等、provider/stage 全体の健全性問題)発生時に TTL 付きで
立て、TTL の間は backfill 再投入を停止する。真実ではなく「今叩くな」のソフトフラグ —
消えても次 batch が少し失敗して再 set される。Redis 障害時は fail-open(救済を止めない)。

Phase 4: hold set 成功 / 失敗を Logfire metric_counter で計測する
(``vector.curation.hold_set`` / ``vector.curation.hold_set_failed``)。
dashboard で「過去 1h に terminal_keep が発生したか」を可視化する救済の中心 metric
(``specs/logfire-stage3-rescue-dashboard.md`` §panel 1/2)。

attribute は ``reason`` (= ``AIProvider*Error.CODE`` または ``"unknown"``) のみ。
``article_id`` や URL を attribute に乗せない構造的契約 (低 cardinality + PII 隔離
oracle: ``tests/test_curation_hold_metrics.py``)。
"""

from __future__ import annotations

import logfire
import structlog
from redis.asyncio import Redis

logger = structlog.get_logger(__name__)

_HOLD_KEY = "curation:hold"
_HOLD_TTL_SECONDS = 6 * 60 * 60  # 6h

# module-level singleton。logfire の Proxy 遅延束縛により、token 未設定時 (dev/CI/test)
# は実 MeterProvider 不在で ``add()`` は no-op、token 投入後の token 有効プロセス
# (staging/prod) では実 exporter に流れる。configure 前 instantiate でも遅延束縛で
# 後付け provider に追従する (Phase 3 設計判断 B と同思想)。
_hold_set_counter = logfire.metric_counter(
    "vector.curation.hold_set",
    unit="1",
    description="Curation hold が新規 set された回数 (terminal_keep の救済発火)",
)
_hold_set_failed_counter = logfire.metric_counter(
    "vector.curation.hold_set_failed",
    unit="1",
    description="Curation hold の set が Redis 障害等で失敗した回数",
)


async def set_curation_hold(redis: Redis, *, reason: str) -> None:
    """terminal_keep 検出時に hold を TTL 付きで立てる(best-effort)。

    Phase 4: 成功時は ``vector.curation.hold_set``、Redis 障害時は
    ``vector.curation.hold_set_failed`` を increment。``attributes={"reason": ...}``
    は ``AIProvider*Error.CODE`` 由来の低 cardinality 固定値で、article_id 等の
    動的値を attribute に乗せない構造的契約。
    """
    try:
        await redis.set(_HOLD_KEY, reason, ex=_HOLD_TTL_SECONDS)
    except Exception:  # noqa: BLE001 — hold は真実でない、set 失敗で業務を壊さない
        _hold_set_failed_counter.add(1, attributes={"reason": reason})
        logger.warning("curation_hold_set_failed", reason=reason, exc_info=True)
        return
    _hold_set_counter.add(1, attributes={"reason": reason})


async def is_curation_held(redis: Redis) -> bool:
    """hold が立っているかを返す。Redis 障害時は fail-open(救済を止めない)。"""
    try:
        return bool(await redis.exists(_HOLD_KEY))
    except Exception:  # noqa: BLE001 — Redis 障害は fail-open(救済を止めない)
        logger.warning("curation_hold_check_failed", exc_info=True)
        return False
