"""パイプライン用タスクキューの broker 定義と共通基盤。

broker:
  - broker_metadata:  RSS/HN メタデータ取得 + dispatch
  - broker_content:   記事単位のコンテンツ抽出
  - broker_analysis:  AI 分析
  - broker_embedding: ベクトル埋め込み生成
  - broker_digest:    週次トレンド snapshot 生成 (cron 駆動)

Workers: broker ごとに 1 つ（docker-compose.yml を参照）。
Scheduler:
  - taskiq scheduler app.brokers:scheduler_metadata (back-fill 用 cron)
  - taskiq scheduler app.brokers:scheduler_digest (週次 snapshot 用 cron)
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

from app.analysis.classifier.deepseek import DeepSeekClassifier
from app.analysis.embedder.ruri import RuriEmbedder
from app.analysis.extraction.extractor.gemini import GeminiExtractor
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
broker_digest = _make_broker("digest")

# ---------------------------------------------------------------------------
# Scheduler — cron 駆動を持つ broker ごとに 1 つ
# ---------------------------------------------------------------------------

scheduler_metadata = TaskiqScheduler(
    broker=broker_metadata,
    sources=[LabelScheduleSource(broker_metadata)],
)
scheduler_digest = TaskiqScheduler(
    broker=broker_digest,
    sources=[LabelScheduleSource(broker_digest)],
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
_register_lifecycle(broker_digest, "digest")

# ---------------------------------------------------------------------------
# AI アダプター wiring — broker_analysis 専用 composition root
# ---------------------------------------------------------------------------
# Provider 選択は本ファイルで hardcode する設計（Pure DI）。切替は env 変更
# ではなくコード変更 + worker restart。Stage 1 と Stage 2 で別の抽象を別の
# クラスに紐付けるため、共有 env による誤切替の余地が構造的に生じない。


@broker_analysis.on_event(TaskiqEvents.WORKER_STARTUP)
async def _wire_analysis_adapters(state: TaskiqState) -> None:
    """Stage 1 / Stage 2 の AI アダプターを worker 起動時に構築する。"""
    state.extractor = GeminiExtractor()
    state.classifier = DeepSeekClassifier()
    logger.info(
        "analysis_adapters_wired",
        extractor=type(state.extractor).__name__,
        extractor_model=state.extractor.MODEL,
        classifier=type(state.classifier).__name__,
        classifier_model=state.classifier.MODEL,
    )


@broker_embedding.on_event(TaskiqEvents.WORKER_STARTUP)
async def _wire_embedding_adapters(state: TaskiqState) -> None:
    """Stage E の embedder アダプターを worker 起動時に構築する。"""
    state.embedder = RuriEmbedder(base_url=settings.embedding_base_url)
    logger.info(
        "embedding_adapters_wired",
        embedder=type(state.embedder).__name__,
        embedder_model=state.embedder.MODEL,
    )


# scheduler に cron を登録するため、import で副作用を起こす。
import app.digest.tasks.snapshot  # noqa: E402, F401
import app.maintenance.tasks  # noqa: E402, F401

# ---------------------------------------------------------------------------
# ヘルパー（タスクモジュール間で共有）
# ---------------------------------------------------------------------------


def is_last_attempt(ctx: Context) -> bool:
    """この試行後に SimpleRetryMiddleware がリトライしない場合 True を返す。"""
    labels = ctx.message.labels
    retry_count = int(labels.get("retry_count", 0))
    max_retries = int(labels.get("max_retries", 0))
    return retry_count >= max_retries
