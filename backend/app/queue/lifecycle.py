"""broker / scheduler の lifecycle event hook を attach する。

本 module を import するだけで broker × 6 + scheduler broker × 3 に対する
WORKER_STARTUP / WORKER_SHUTDOWN / CLIENT_STARTUP / CLIENT_SHUTDOWN hook が
登録される (副作用)。AI adapter wiring (Pure DI composition root) は本 module
ではなく ``composition.py`` の責務。本 module は engine 生成 / Logfire bootstrap /
SQLAlchemy instrument の汎用 lifecycle のみ。
"""

from __future__ import annotations

import logfire
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from taskiq import TaskiqEvents, TaskiqState
from taskiq_redis import RedisStreamBroker

from app.config import settings
from app.logfire_setup import setup_logfire
from app.queue.brokers import (
    broker_analysis,
    broker_briefing,
    broker_content,
    broker_digest,
    broker_embedding,
    broker_metadata,
)

logger = structlog.get_logger(__name__)


def _register_worker_lifecycle(broker: RedisStreamBroker, label: str) -> None:
    @broker.on_event(TaskiqEvents.WORKER_STARTUP)
    async def on_startup(state: TaskiqState) -> None:
        # 可観測性 bootstrap は engine 生成や追加 startup hook
        # (composition._wire_*_adapters) より先に走らせ、それらのログも structlog →
        # Logfire 経路に乗るようにする。各 worker プロセスでは自分の broker の
        # on_startup だけが発火するため、プロセスごとに正しい service_name で
        # 1 回ずつ呼ばれる。
        setup_logfire(f"vector-worker-{label}")
        state.engine = create_async_engine(settings.database_url, echo=False)
        state.session_factory = async_sessionmaker(
            state.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        # worker engine の DB query を 1 query = 1 span として Logfire に乗せる。
        # 各 worker プロセスは自分の broker の on_startup だけが発火するため、
        # プロセスごとに 1 engine が 1 度 instrument される (重複なし)。
        logfire.instrument_sqlalchemy(engine=state.engine)
        logger.info(f"{label}_worker_startup")

    @broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
    async def on_shutdown(state: TaskiqState) -> None:
        if hasattr(state, "engine"):
            await state.engine.dispose()
        logger.info(f"{label}_worker_shutdown")


def _register_scheduler_lifecycle(broker: RedisStreamBroker, label: str) -> None:
    """Scheduler プロセス専用の bootstrap hook を broker に attach する。

    ``broker.startup()`` は ``is_worker_process`` 分岐で WORKER_STARTUP /
    CLIENT_STARTUP を発火する (taskiq.abc.broker)。API プロセスはそもそも
    ``broker.startup()`` を呼ばず ``.kiq()`` は AsyncKicker による lazy 経路なので、
    CLIENT_STARTUP は **scheduler プロセスでのみ発火する** (no gate required)。
    cron 駆動を持つ broker (broker_metadata / broker_digest / broker_briefing) のみ
    に本関数を当てる。content / analysis / embedding broker は scheduler が存在し
    ないため不要。

    Scheduler 自身は DB を触らない (全 cron task は worker 側で実行され、
    state.engine も session_factory も WORKER_STARTUP でしか初期化されない) ため、
    setup_logfire のみで充分 (engine 生成 / instrument_sqlalchemy は意図的に呼ば
    ない)。enqueue 自体の telemetry は OpenTelemetryMiddleware.pre_send が
    PRODUCER span として出す (scheduler process でも middleware は実行される)。
    """

    @broker.on_event(TaskiqEvents.CLIENT_STARTUP)
    async def on_scheduler_startup(state: TaskiqState) -> None:
        setup_logfire(f"vector-scheduler-{label}")
        logger.info(f"{label}_scheduler_startup")

    @broker.on_event(TaskiqEvents.CLIENT_SHUTDOWN)
    async def on_scheduler_shutdown(state: TaskiqState) -> None:
        logger.info(f"{label}_scheduler_shutdown")


_register_worker_lifecycle(broker_metadata, "metadata")
_register_worker_lifecycle(broker_content, "content")
_register_worker_lifecycle(broker_analysis, "analysis")
_register_worker_lifecycle(broker_embedding, "embedding")
_register_worker_lifecycle(broker_digest, "digest")
_register_worker_lifecycle(broker_briefing, "briefing")

# broker_metadata / broker_digest / broker_briefing は worker process と scheduler
# process の両方で同じ broker object を共有するため、_register_worker_lifecycle
# (WORKER_STARTUP) と _register_scheduler_lifecycle (CLIENT_STARTUP) の両方を呼ぶ。
# プロセスが違うのでイベント発火が衝突することはない。
_register_scheduler_lifecycle(broker_metadata, "metadata")
_register_scheduler_lifecycle(broker_digest, "digest")
_register_scheduler_lifecycle(broker_briefing, "briefing")
