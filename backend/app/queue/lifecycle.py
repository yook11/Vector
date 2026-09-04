"""broker / scheduler の lifecycle event hook を attach する。

本 module を import するだけで broker × 8 + scheduler broker × 5 に対する
WORKER_STARTUP / WORKER_SHUTDOWN / CLIENT_STARTUP / CLIENT_SHUTDOWN hook が
登録される (副作用)。AI adapter wiring (Pure DI composition root) は本 module
ではなく ``composition.py`` の責務。Engine / Session factory / 用途別 Redis
client は本 module がプロセス寿命で所有し、Logfire bootstrap と SQLAlchemy
instrument もここで行う。
"""

from __future__ import annotations

import logfire
import structlog
from taskiq import TaskiqEvents, TaskiqState
from taskiq_redis import RedisStreamBroker

from app.config import settings
from app.db.engine import (
    AUTH_RETENTION_MAX_OVERFLOW,
    AUTH_RETENTION_POOL_SIZE,
    DEFAULT_POOL_TIMEOUT,
    WORKER_POOL_RECYCLE_SECONDS,
    WORKER_POOL_SIZING,
    auth_retention_service_name,
    create_auth_retention_engine,
    create_worker_engine,
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
from app.redis import (
    create_worker_agent_live_client,
    create_worker_pipeline_control_client,
)

logger = structlog.get_logger(__name__)

_PIPELINE_CONTROL_WORKER_LABELS = frozenset({"analysis", "embedding", "maintenance"})


def _attach_worker_redis(state: TaskiqState, label: str) -> None:
    """この worker が使う用途の Redis client だけを state に載せる。"""
    if label == "agent":
        state.agent_live_redis = create_worker_agent_live_client(settings)
        return
    if label in _PIPELINE_CONTROL_WORKER_LABELS:
        state.pipeline_control_redis = create_worker_pipeline_control_client(settings)


async def _aclose_worker_redis(state: TaskiqState) -> None:
    live = getattr(state, "agent_live_redis", None)
    if live is not None:
        await live.aclose()
    control = getattr(state, "pipeline_control_redis", None)
    if control is not None:
        await control.aclose()


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
        # override。resilience (pre_ping / pool_timeout) は Engine 共通の
        # 既定 (Neon scale-to-zero 対策)。
        state.engine = create_worker_engine(settings, label)
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
        _attach_worker_redis(state, label)
        if label == "maintenance":
            try:
                state.auth_engine = create_auth_retention_engine(settings)
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
        await _aclose_worker_redis(state)
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
