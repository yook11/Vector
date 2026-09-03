"""broker / scheduler の lifecycle event hook を attach する。

本 module を import するだけで broker × 8 + scheduler broker × 5 に対する
WORKER_STARTUP / WORKER_SHUTDOWN / CLIENT_STARTUP / CLIENT_SHUTDOWN hook が
登録される (副作用)。AI adapter wiring (Pure DI composition root) は本 module
ではなく ``composition.py`` の責務。Engine と Session factory は ``app.db`` の
目録から載せ、本 module は Logfire bootstrap / SQLAlchemy instrument の
lifecycle のみ。
"""

from __future__ import annotations

import logfire
import structlog
from taskiq import TaskiqEvents, TaskiqState
from taskiq_redis import RedisStreamBroker

from app.db.connection import DEFAULT_POOL_TIMEOUT
from app.db.engine import (
    AUTH_RETENTION_MAX_OVERFLOW,
    AUTH_RETENTION_POOL_SIZE,
    WORKER_POOL_RECYCLE_SECONDS,
    WORKER_POOL_SIZING,
    auth_retention_service_name,
    build_auth_retention_engine,
    build_worker_engine,
    worker_service_name,
)
from app.db.session import caller_managed_session_factory
from app.logfire.db_pool import log_pool_initialized, register_pool_metrics
from app.logfire.setup import setup_logfire
from app.queue.brokers import (
    broker_agent,
    broker_analysis,
    broker_briefing,
    broker_collection,
    broker_dispatch,
    broker_embedding,
    broker_maintenance,
    broker_trend_discovery,
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
        service_name = worker_service_name(label)
        setup_logfire(service_name)
        # pool sizing は WORKER_POOL_SIZING (label 別)、recycle=240 で worker のみ
        # override。resilience (pre_ping / pool_timeout) は create_app_engine の
        # 既定 (Neon scale-to-zero 対策)。
        state.engine = build_worker_engine(label)
        state.session_factory = caller_managed_session_factory(state.engine)
        # worker engine の DB query を 1 query = 1 span として Logfire に乗せる。
        # 各 worker プロセスは自分の broker の on_startup だけが発火するため、
        # プロセスごとに 1 engine が 1 度 instrument される (重複なし)。
        logfire.instrument_sqlalchemy(engine=state.engine)
        pool_size, max_overflow = WORKER_POOL_SIZING[label]
        log_pool_initialized(
            service_name=service_name,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_recycle=WORKER_POOL_RECYCLE_SECONDS,
            pool_timeout=DEFAULT_POOL_TIMEOUT,
        )
        register_pool_metrics(
            state.engine, pool_size=pool_size, max_overflow=max_overflow
        )
        if label == "maintenance":
            try:
                state.auth_engine = build_auth_retention_engine()
            except RuntimeError as exc:
                logger.error(
                    "maintenance_auth_retention_engine_missing",
                    error_type=exc.__class__.__name__,
                )
            except Exception as exc:
                logger.error(
                    "maintenance_auth_retention_engine_failed",
                    error_type=exc.__class__.__name__,
                )
            else:
                state.auth_session_factory = caller_managed_session_factory(
                    state.auth_engine
                )
                logfire.instrument_sqlalchemy(engine=state.auth_engine)
                log_pool_initialized(
                    service_name=auth_retention_service_name(),
                    pool_size=AUTH_RETENTION_POOL_SIZE,
                    max_overflow=AUTH_RETENTION_MAX_OVERFLOW,
                    pool_recycle=WORKER_POOL_RECYCLE_SECONDS,
                    pool_timeout=DEFAULT_POOL_TIMEOUT,
                )
                register_pool_metrics(
                    state.auth_engine,
                    pool_size=AUTH_RETENTION_POOL_SIZE,
                    max_overflow=AUTH_RETENTION_MAX_OVERFLOW,
                )
        logger.info(f"{label}_worker_startup")

        if label == "analysis":
            # enum↔categories seed のドリフトを起動時に fail-fast 検出する
            # (lazy import で broker wiring の import 順序に影響させない)。
            from app.analysis.assessment.repository import AssessmentRepository

            async with state.session_factory() as session:
                await AssessmentRepository(
                    session
                ).assert_category_catalog_covers_enum()

    @broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
    async def on_shutdown(state: TaskiqState) -> None:
        if hasattr(state, "auth_engine"):
            await state.auth_engine.dispose()
        if hasattr(state, "engine"):
            await state.engine.dispose()
        logger.info(f"{label}_worker_shutdown")


def _register_scheduler_lifecycle(broker: RedisStreamBroker, label: str) -> None:
    """Scheduler プロセス専用の bootstrap hook を broker に attach する。

    ``broker.startup()`` は ``is_worker_process`` 分岐で WORKER_STARTUP /
    CLIENT_STARTUP を発火する (taskiq.abc.broker)。API プロセスはそもそも
    ``broker.startup()`` を呼ばず ``.kiq()`` は AsyncKicker による lazy 経路なので、
    CLIENT_STARTUP は **scheduler プロセスでのみ発火する** (no gate required)。
    cron 駆動を持つ broker (broker_dispatch / broker_trend_discovery /
    broker_briefing / broker_agent / broker_maintenance) のみに本関数を当てる。
    collection / analysis / embedding broker は scheduler が存在しないため不要。

    Scheduler 自身は DB を触らない (全 cron task は worker 側で実行され、
    state.engine も session_factory も WORKER_STARTUP でしか初期化されない) ため、
    本 hook は startup/shutdown ログのみを担う (engine 生成 / instrument_sqlalchemy は
    意図的に呼ばない)。

    Logfire bootstrap は本 hook では呼ばない。統合後の scheduler は 1 プロセスで 5
    broker の CLIENT_STARTUP が走るため、hook 内で ``setup_logfire`` を呼ぶと
    ``logfire.instrument_httpx`` (global patch) が 5 回積み重なり「プロセスごとに 1 度」
    契約 (test_logfire_setup) を破る。よって ``setup_logfire("vector-scheduler")`` は
    entrypoint (``scheduler_entrypoint._main``) が process 先頭で 1 度だけ呼ぶ
    (API が lifespan で 1 度呼ぶのと同パターン)。enqueue 自体の telemetry は
    OpenTelemetryMiddleware.pre_send が PRODUCER span として出す (scheduler process
    でも middleware は実行される)。
    """

    @broker.on_event(TaskiqEvents.CLIENT_STARTUP)
    async def on_scheduler_startup(state: TaskiqState) -> None:
        logger.info(f"{label}_scheduler_startup")

    @broker.on_event(TaskiqEvents.CLIENT_SHUTDOWN)
    async def on_scheduler_shutdown(state: TaskiqState) -> None:
        logger.info(f"{label}_scheduler_shutdown")


_register_worker_lifecycle(broker_dispatch, "dispatch")
_register_worker_lifecycle(broker_collection, "collection")
_register_worker_lifecycle(broker_analysis, "analysis")
_register_worker_lifecycle(broker_embedding, "embedding")
_register_worker_lifecycle(broker_trend_discovery, "trend_discovery")
_register_worker_lifecycle(broker_briefing, "briefing")
_register_worker_lifecycle(broker_agent, "agent")
_register_worker_lifecycle(broker_maintenance, "maintenance")

# broker_dispatch / broker_trend_discovery / broker_briefing / broker_agent /
# broker_maintenance は
# worker process と scheduler process の両方で同じ broker object を共有するため、
# _register_worker_lifecycle (WORKER_STARTUP) と _register_scheduler_lifecycle
# (CLIENT_STARTUP) の両方を呼ぶ。
# プロセスが違うのでイベント発火が衝突することはない。
_register_scheduler_lifecycle(broker_dispatch, "dispatch")
_register_scheduler_lifecycle(broker_trend_discovery, "trend_discovery")
_register_scheduler_lifecycle(broker_briefing, "briefing")
_register_scheduler_lifecycle(broker_agent, "agent")
_register_scheduler_lifecycle(broker_maintenance, "maintenance")
