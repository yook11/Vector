"""broker / scheduler の lifecycle event hook を attach する。

本 module を import するだけで共通catalogのbroker × 7に対する
WORKER_STARTUP / WORKER_SHUTDOWN / CLIENT_STARTUP / CLIENT_SHUTDOWN hook が
登録される (副作用)。broker ごとの Redis 用途と AI adapter 配線は
``WorkerRuntime`` に集約し、単一 startup が順に実行する。AI provider の具象選択
は ``composition.py`` の責務。Engine / Session factory / 用途別 Redis client は
本 module がプロセス寿命で所有し、Logfire bootstrap と SQLAlchemy instrument も
ここで行う。
"""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Literal

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
)
from app.queue.composition import (
    _warm_agent_sdk_imports,
    _wire_analysis_adapters,
    _wire_briefing_adapter,
    _wire_embedding_adapters,
)
from app.redis import (
    create_worker_agent_live_client,
    create_worker_pipeline_control_client,
)

logger = structlog.get_logger(__name__)

type _RedisFactory = Callable[..., object]
type _Compose = Callable[[TaskiqState], Awaitable[None]]


@dataclass(frozen=True)
class WorkerRuntime:
    """1 broker の worker 起動に必要な label / Redis / adapter 配線。"""

    label: str
    redis_factory: _RedisFactory | None = None
    redis_attr: Literal["pipeline_control_redis", "agent_live_redis"] | None = None
    compose: _Compose | None = None

    def __post_init__(self) -> None:
        if (self.redis_factory is None) != (self.redis_attr is None):
            raise ValueError("redis_factory and redis_attr must be paired")


def _attach_worker_redis(state: TaskiqState, runtime: WorkerRuntime) -> None:
    """この worker が使う用途の Redis client だけを state に載せる。"""
    if runtime.redis_factory is None or runtime.redis_attr is None:
        return
    # 表に閉じ込めた参照ではなく module 属性を使い、test の patch を受け取る。
    factory = getattr(sys.modules[__name__], runtime.redis_factory.__name__)
    setattr(state, runtime.redis_attr, factory(settings))


async def _compose(runtime: WorkerRuntime, state: TaskiqState) -> None:
    if runtime.compose is not None:
        await runtime.compose(state)


async def _aclose_worker_resources(state: TaskiqState) -> None:
    """所有 resource を全部閉じ、失敗しても残りを試してから再送出する。"""
    async with AsyncExitStack() as stack:
        if hasattr(state, "engine"):
            stack.push_async_callback(state.engine.dispose)
        if hasattr(state, "auth_engine"):
            stack.push_async_callback(state.auth_engine.dispose)
        live = getattr(state, "agent_live_redis", None)
        if live is not None:
            stack.push_async_callback(live.aclose)
        control = getattr(state, "pipeline_control_redis", None)
        if control is not None:
            stack.push_async_callback(control.aclose)


def _register_worker_lifecycle(
    broker: RedisStreamBroker, runtime: WorkerRuntime
) -> None:
    label = runtime.label

    @broker.on_event(TaskiqEvents.WORKER_STARTUP)
    async def on_startup(state: TaskiqState) -> None:
        # 可観測性 bootstrap は engine 生成や compose より先に走らせ、それらの
        # ログも structlog → Logfire 経路に乗るようにする。各 worker プロセスでは
        # 自分の broker の on_startup だけが発火するため、プロセスごとに正しい
        # service_name で 1 回ずつ呼ばれる。
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
        _attach_worker_redis(state, runtime)
        await _compose(runtime, state)
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
        await _aclose_worker_resources(state)
        logger.info(f"{label}_worker_shutdown")


def _register_client_lifecycle(broker: RedisStreamBroker, label: str) -> None:
    """enqueue 側 (API / scheduler) の CLIENT_* hook を broker に attach する。

    ``broker.startup()`` は ``is_worker_process`` 分岐で WORKER_STARTUP /
    CLIENT_STARTUP を発火する (taskiq.abc.broker)。API と scheduler はどちらも
    worker ではないので CLIENT_* が走る。cron 駆動を持つ broker
    (broker_dispatch / broker_briefing / broker_agent /
    broker_maintenance) のみに本関数を当てる。collection は API が producer
    として startup するが cron が無い。analysis / embedding は scheduler も
    API producer も無い。

    enqueue 側は DB を触らない (engine / session_factory は WORKER_STARTUP のみ)
    ため、本 hook は startup/shutdown ログだけを担う。

    Logfire bootstrap は本 hook では呼ばない。scheduler は 1 プロセスで 5 broker
    の CLIENT_STARTUP が走るため、hook 内で ``setup_logfire`` を呼ぶと
    ``logfire.instrument_httpx`` (global patch) が 5 回積み重なり「プロセスごとに
    1 度」契約 (test_logfire_setup) を破る。API は lifespan、scheduler は
    entrypoint が process 先頭で 1 度だけ呼ぶ。scheduler 固有の識別は
    ``setup_logfire("vector-scheduler")`` が持つ。enqueue 自体の telemetry は
    OpenTelemetryMiddleware.pre_send が PRODUCER span として出す。
    """

    @broker.on_event(TaskiqEvents.CLIENT_STARTUP)
    async def on_client_startup(state: TaskiqState) -> None:
        logger.info(f"{label}_client_startup")

    @broker.on_event(TaskiqEvents.CLIENT_SHUTDOWN)
    async def on_client_shutdown(state: TaskiqState) -> None:
        logger.info(f"{label}_client_shutdown")


_register_worker_lifecycle(broker_dispatch, WorkerRuntime("dispatch"))
_register_worker_lifecycle(broker_collection, WorkerRuntime("collection"))
_register_worker_lifecycle(
    broker_analysis,
    WorkerRuntime(
        "analysis",
        redis_factory=create_worker_pipeline_control_client,
        redis_attr="pipeline_control_redis",
        compose=_wire_analysis_adapters,
    ),
)
_register_worker_lifecycle(
    broker_embedding,
    WorkerRuntime(
        "embedding",
        redis_factory=create_worker_pipeline_control_client,
        redis_attr="pipeline_control_redis",
        compose=_wire_embedding_adapters,
    ),
)
_register_worker_lifecycle(
    broker_briefing,
    WorkerRuntime("briefing", compose=_wire_briefing_adapter),
)
_register_worker_lifecycle(
    broker_agent,
    WorkerRuntime(
        "agent",
        redis_factory=create_worker_agent_live_client,
        redis_attr="agent_live_redis",
        compose=_warm_agent_sdk_imports,
    ),
)
_register_worker_lifecycle(
    broker_maintenance,
    WorkerRuntime(
        "maintenance",
        redis_factory=create_worker_pipeline_control_client,
        redis_attr="pipeline_control_redis",
    ),
)

# broker_dispatch / broker_briefing / broker_agent /
# broker_maintenance は worker と enqueue 側 (API / scheduler) で同じ broker
# object を共有するため、_register_worker_lifecycle (WORKER_STARTUP) と
# _register_client_lifecycle (CLIENT_STARTUP) の両方を呼ぶ。
# プロセスが違うのでイベント発火が衝突することはない。
_register_client_lifecycle(broker_dispatch, "dispatch")
_register_client_lifecycle(broker_briefing, "briefing")
_register_client_lifecycle(broker_agent, "agent")
_register_client_lifecycle(broker_maintenance, "maintenance")
