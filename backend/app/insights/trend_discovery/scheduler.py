"""Trend Discoveryを定期的にenqueueするscheduler factory。"""

from __future__ import annotations

import structlog
from taskiq import TaskiqEvents, TaskiqScheduler, TaskiqState
from taskiq.schedule_sources import LabelScheduleSource
from taskiq_redis import RedisStreamBroker

from app.config import settings
from app.insights.trend_discovery.taskiq_job import register_trend_discovery_task
from app.redis.clients import taskiq_stream_connection
from app.redis.taskiq_stream_broker import create_taskiq_stream_broker

logger = structlog.get_logger(__name__)

_QUEUE_NAME = "trend_discovery"


def _attach_client_lifecycle(broker: RedisStreamBroker) -> None:
    @broker.on_event(TaskiqEvents.CLIENT_STARTUP)
    async def on_startup(_state: TaskiqState) -> None:
        logger.info("trend_discovery_client_startup")

    @broker.on_event(TaskiqEvents.CLIENT_SHUTDOWN)
    async def on_shutdown(_state: TaskiqState) -> None:
        logger.info("trend_discovery_client_shutdown")


def create_scheduler() -> TaskiqScheduler:
    """scheduler processが所有するproducer brokerとcron登録を構築する。"""
    broker = create_taskiq_stream_broker(
        taskiq_stream_connection(settings),
        _QUEUE_NAME,
    )
    register_trend_discovery_task(broker)
    _attach_client_lifecycle(broker)
    return TaskiqScheduler(
        broker=broker,
        sources=[LabelScheduleSource(broker)],
    )
