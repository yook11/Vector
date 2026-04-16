"""パイプライン用タスクキューの broker 定義と共通基盤。

パイプラインの各ステップに対応する 4 つの broker:
  - broker_metadata:  RSS/HN メタデータ取得 + dispatch
  - broker_content:   記事単位のコンテンツ抽出
  - broker_analysis:  AI 分析
  - broker_embedding: ベクトル埋め込み生成

Workers: broker ごとに 1 つ（docker-compose.yml を参照）。
Scheduler: taskiq scheduler app.tasks.brokers:scheduler_metadata
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession
from taskiq import (
    Context,
    SimpleRetryMiddleware,
    TaskiqEvents,
    TaskiqScheduler,
    TaskiqState,
)
from taskiq.schedule_sources import LabelScheduleSource
from taskiq_redis import RedisAsyncResultBackend, RedisStreamBroker

from app.config import settings

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# settings から導出する cron スケジュール
# ---------------------------------------------------------------------------

_VALID_INTERVAL_MINUTES = {5, 10, 15, 20, 30, 60}
if settings.check_interval_minutes not in _VALID_INTERVAL_MINUTES:
    raise ValueError(
        f"check_interval_minutes={settings.check_interval_minutes} "
        f"is not a divisor of 60. "
        f"Valid values: {sorted(_VALID_INTERVAL_MINUTES)}"
    )
if settings.check_interval_minutes == 60:
    _FETCH_CRON = "0 * * * *"
else:
    _FETCH_CRON = f"*/{settings.check_interval_minutes} * * * *"

# ---------------------------------------------------------------------------
# Broker factory
# ---------------------------------------------------------------------------


def _make_broker(queue_name: str) -> RedisStreamBroker:
    return (
        RedisStreamBroker(
            url=settings.redis_url,
            idle_timeout=600_000,
            maxlen=10_000,
            queue_name=queue_name,
        )
        .with_result_backend(
            RedisAsyncResultBackend(
                redis_url=settings.redis_url,
                result_ex_time=3600,
            )
        )
        .with_middlewares(SimpleRetryMiddleware(default_retry_count=0))
    )


broker_metadata = _make_broker("pipeline:metadata")
broker_content = _make_broker("pipeline:content")
broker_analysis = _make_broker("pipeline:analysis")
broker_embedding = _make_broker("pipeline:embedding")

# ---------------------------------------------------------------------------
# Scheduler（metadata broker のみ — cron タスクは fetch_metadata だけ）
# ---------------------------------------------------------------------------

scheduler_metadata = TaskiqScheduler(
    broker=broker_metadata,
    sources=[LabelScheduleSource(broker_metadata)],
)

# ---------------------------------------------------------------------------
# ライフサイクルフック — broker ごとに独自の engine を持つ
# ---------------------------------------------------------------------------


def _register_lifecycle(broker: RedisStreamBroker, label: str) -> None:
    @broker.on_event(TaskiqEvents.WORKER_STARTUP)
    async def on_startup(state: TaskiqState) -> None:
        state.engine = create_async_engine(settings.database_url, echo=False)
        state.session_factory = async_sessionmaker(
            state.engine,
            class_=SQLModelAsyncSession,
            expire_on_commit=False,
        )
        logger.info(f"{label}_worker_startup")

    @broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
    async def on_shutdown(state: TaskiqState) -> None:
        if hasattr(state, "engine"):
            await state.engine.dispose()
        logger.info(f"{label}_worker_shutdown")


_register_lifecycle(broker_metadata, "metadata")
_register_lifecycle(broker_content, "content")
_register_lifecycle(broker_analysis, "analysis")
_register_lifecycle(broker_embedding, "embedding")

# ---------------------------------------------------------------------------
# ヘルパー（タスクモジュール間で共有）
# ---------------------------------------------------------------------------


def is_last_attempt(ctx: Context) -> bool:
    """この試行後に SimpleRetryMiddleware がリトライしない場合 True を返す。"""
    labels = ctx.message.labels
    retry_count = int(labels.get("retry_count", 0))
    max_retries = int(labels.get("max_retries", 0))
    return retry_count >= max_retries
