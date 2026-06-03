"""DB コネクションプールの起動ログと Logfire metrics を登録する。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import cast

import logfire
import structlog
from opentelemetry.metrics import CallbackOptions, Observation
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import QueuePool

logger = structlog.get_logger(__name__)


def log_pool_initialized(
    *,
    service_name: str,
    pool_size: int,
    max_overflow: int,
    pool_recycle: int,
    pool_timeout: int,
) -> None:
    """起動時に pool profile を 1 度だけ構造化ログする。

    ``capacity`` は飽和率 checked_out / capacity の分母になる。
    """
    logger.info(
        "db_pool_initialized",
        service=service_name,
        pool_size=pool_size,
        max_overflow=max_overflow,
        capacity=pool_size + max_overflow,
        pool_recycle=pool_recycle,
        pool_timeout=pool_timeout,
    )


def pool_stats(engine: AsyncEngine) -> dict[str, int]:
    """engine の live pool の現在値を読む。

    dispose() は pool を作り替えるため engine から都度取得し stale を避ける。
    overflow は負値が正常 (常駐枠が未使用)。runtime pool は QueuePool 系。
    """
    pool = cast(QueuePool, engine.sync_engine.pool)
    return {"checked_out": pool.checkedout(), "overflow": pool.overflow()}


def register_pool_metrics(
    engine: AsyncEngine, *, pool_size: int, max_overflow: int
) -> None:
    """pool を 60s 間隔でサンプリングする observable gauge を登録する。

    service.name resource attribute でプロセスを分離するため、metric 側に
    attribute は付けない。
    """
    capacity = pool_size + max_overflow

    def _checked_out(_options: CallbackOptions) -> Iterable[Observation]:
        yield Observation(pool_stats(engine)["checked_out"])

    def _overflow(_options: CallbackOptions) -> Iterable[Observation]:
        yield Observation(pool_stats(engine)["overflow"])

    def _capacity(_options: CallbackOptions) -> Iterable[Observation]:
        yield Observation(capacity)

    logfire.metric_gauge_callback(
        "vector.db.pool.checked_out",
        [_checked_out],
        unit="1",
        description="貸出中の DB 接続数 (sampled)",
    )
    logfire.metric_gauge_callback(
        "vector.db.pool.overflow",
        [_overflow],
        unit="1",
        description="overflow 現在値 (負=未使用 / 正=溢れ, sampled)",
    )
    logfire.metric_gauge_callback(
        "vector.db.pool.capacity",
        [_capacity],
        unit="1",
        description="pool_size + max_overflow (静的上限)",
    )
