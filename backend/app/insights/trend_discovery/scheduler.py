"""Trend Discoveryを定期的にenqueueするscheduler factory。"""

from __future__ import annotations

from taskiq import TaskiqEvents, TaskiqScheduler, TaskiqState
from taskiq.schedule_sources import LabelScheduleSource
from taskiq_redis import RedisStreamBroker

from app.config import settings
from app.insights.trend_discovery.taskiq_job import register_trend_discovery_task
from app.log_policy.policies.process_runtime import PROCESS_RUNTIME_LOG_RULES
from app.log_policy.runtime import create_policy_json_logger
from app.redis.clients import taskiq_stream_connection
from app.redis.taskiq_stream_broker import create_taskiq_stream_broker

logger = create_policy_json_logger(__name__, PROCESS_RUNTIME_LOG_RULES)

_QUEUE_NAME = "trend_discovery"


def _attach_client_lifecycle(broker: RedisStreamBroker) -> None:
    @broker.on_event(TaskiqEvents.CLIENT_STARTUP)
    async def on_startup(_state: TaskiqState) -> None:
        logger.info("client_startup", broker=_QUEUE_NAME)

    @broker.on_event(TaskiqEvents.CLIENT_SHUTDOWN)
    async def on_shutdown(_state: TaskiqState) -> None:
        logger.info("client_shutdown", broker=_QUEUE_NAME)


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
