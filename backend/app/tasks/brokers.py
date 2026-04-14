"""Broker definitions and shared infrastructure for pipeline task queues.

4 brokers, one per pipeline step:
  - broker_metadata:  RSS/HN metadata fetch + dispatch
  - broker_content:   per-article content extraction
  - broker_analysis:  AI analysis
  - broker_embedding: vector embedding generation

Workers: one per broker (see docker-compose.yml).
Scheduler: taskiq scheduler app.tasks.brokers:scheduler_metadata
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import create_async_engine
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
# Cron schedule derived from settings
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
# Scheduler (metadata broker only — fetch_metadata is the only cron task)
# ---------------------------------------------------------------------------

scheduler_metadata = TaskiqScheduler(
    broker=broker_metadata,
    sources=[LabelScheduleSource(broker_metadata)],
)

# ---------------------------------------------------------------------------
# Lifecycle hooks — each broker gets its own engine
# ---------------------------------------------------------------------------


def _register_lifecycle(broker: RedisStreamBroker, label: str) -> None:
    @broker.on_event(TaskiqEvents.WORKER_STARTUP)
    async def on_startup(state: TaskiqState) -> None:
        state.engine = create_async_engine(settings.database_url, echo=False)
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
# Helpers (shared across task modules)
# ---------------------------------------------------------------------------


def is_last_attempt(ctx: Context) -> bool:
    """Return True if SimpleRetryMiddleware will not retry after this attempt."""
    labels = ctx.message.labels
    retry_count = int(labels.get("retry_count", 0))
    max_retries = int(labels.get("max_retries", 0))
    return retry_count >= max_retries
