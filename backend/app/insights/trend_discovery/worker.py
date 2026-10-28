"""Trend Discovery worker applicationのcomposition root。"""

from __future__ import annotations

import logfire
from taskiq import TaskiqEvents, TaskiqState
from taskiq_redis import RedisStreamBroker

from app.config import settings
from app.db.engine import (
    DEFAULT_POOL_RECYCLE,
    DEFAULT_POOL_TIMEOUT,
    WORKER_POOL_SIZING,
    create_worker_engine,
    worker_service_name,
)
from app.db.session import caller_managed_session_factory
from app.insights.trend_discovery.taskiq_job import register_trend_discovery_task
from app.log_policy.policies.process_runtime import PROCESS_RUNTIME_LOG_RULES
from app.log_policy.runtime import create_policy_json_logger
from app.logfire.db_pool import log_pool_initialized, register_pool_metrics
from app.logfire.setup import setup_logfire
from app.redis.clients import taskiq_stream_connection
from app.redis.taskiq_stream_broker import create_taskiq_stream_broker

logger = create_policy_json_logger(__name__, PROCESS_RUNTIME_LOG_RULES)

_WORKER_LABEL = "trend_discovery"
_QUEUE_NAME = "trend_discovery"


def _attach_worker_resources(broker: RedisStreamBroker) -> None:
    @broker.on_event(TaskiqEvents.WORKER_STARTUP)
    async def on_startup(state: TaskiqState) -> None:
        service_name = worker_service_name(_WORKER_LABEL)
        setup_logfire(service_name)
        engine = create_worker_engine(settings, _WORKER_LABEL)
        try:
            session_factory = caller_managed_session_factory(engine)
            logfire.instrument_sqlalchemy(engine=engine)
            pool_size, max_overflow = WORKER_POOL_SIZING[_WORKER_LABEL]
            log_pool_initialized(
                service_name=service_name,
                pool_size=pool_size,
                max_overflow=max_overflow,
                pool_recycle=DEFAULT_POOL_RECYCLE,
                pool_timeout=DEFAULT_POOL_TIMEOUT,
            )
            register_pool_metrics(
                engine,
                pool_size=pool_size,
                max_overflow=max_overflow,
            )
        except BaseException:
            try:
                await engine.dispose()
            except Exception as exc:
                logger.error(
                    "worker_startup_cleanup_failed",
                    broker=_WORKER_LABEL,
                    exc_info=exc,
                )
            raise
        state.engine = engine
        state.session_factory = session_factory
        logger.info("worker_startup", broker=_WORKER_LABEL)

    @broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
    async def on_shutdown(state: TaskiqState) -> None:
        engine = getattr(state, "engine", None)
        if engine is not None:
            await engine.dispose()
        logger.info("worker_shutdown", broker=_WORKER_LABEL)


def create_broker() -> RedisStreamBroker:
    """このworkerが所有するbrokerとtask、resource lifecycleを構築する。"""
    broker = create_taskiq_stream_broker(
        taskiq_stream_connection(settings),
        _QUEUE_NAME,
    )
    register_trend_discovery_task(broker)
    _attach_worker_resources(broker)
    return broker
