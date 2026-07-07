"""broker / scheduler の lifecycle event hook を attach する。

本 module を import するだけで broker × 7 + scheduler broker × 4 に対する
WORKER_STARTUP / WORKER_SHUTDOWN / CLIENT_STARTUP / CLIENT_SHUTDOWN hook が
登録される (副作用)。AI adapter wiring (Pure DI composition root) は本 module
ではなく ``composition.py`` の責務。本 module は engine 生成 / Logfire bootstrap /
SQLAlchemy instrument の汎用 lifecycle のみ。
"""

from __future__ import annotations

import logfire
import structlog
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from taskiq import TaskiqEvents, TaskiqState
from taskiq_redis import RedisStreamBroker

from app.config import settings
from app.db_ssl import DEFAULT_POOL_TIMEOUT, create_app_engine
from app.logfire.db_pool import log_pool_initialized, register_pool_metrics
from app.logfire.setup import setup_logfire
from app.queue.brokers import (
    broker_analysis,
    broker_briefing,
    broker_content,
    broker_embedding,
    broker_maintenance,
    broker_metadata,
    broker_trend_discovery,
)

logger = structlog.get_logger(__name__)

# worker engine の pool sizing (label -> (pool_size, max_overflow))。
# 均一既定 (5,5)=cap10。trend_discovery のみ日次 cron・fan-out なし・
# 最大 1 connection のため縮小 (2,2)=cap4。
# supervisord の --max-async-tasks は該当 worker の cap 以下に保つ
# (通常パスの上限ガード、tests/test_brokers.py が pin する)。error-path で
# 別 audit session を開く経路 (acquisition の変換棄却 / curation の
# ready-build 失敗) があり飽和不可能の保証ではない。二重 audit 分は
# max_overflow + pool_timeout fail-fast で吸収する。
WORKER_POOL_SIZING: dict[str, tuple[int, int]] = {
    "metadata": (5, 5),
    "content": (5, 5),
    "analysis": (5, 5),
    "embedding": (5, 5),
    "trend_discovery": (2, 2),
    "briefing": (5, 5),
    "maintenance": (5, 5),
}
# Neon autosuspend (既定 300s) の手前で接続を張り替え、pre_ping 依存を
# 減らす (60s マージン)。create_app_engine の factory 既定 (3600) を worker
# のみ override する (API は 3600 据え置き)。
WORKER_POOL_RECYCLE_SECONDS = 240
AUTH_RETENTION_POOL_SIZE = 1
AUTH_RETENTION_MAX_OVERFLOW = 1


def worker_service_name(label: str) -> str:
    """worker プロセスの service 名 (= asyncpg application_name) を返す。"""
    return f"vector-worker-{label}"


def build_worker_engine(label: str) -> AsyncEngine:
    """``label`` の sizing で worker engine を作る (hook とテストの共用入口)。

    resilience (pre_ping / pool_timeout) は ``create_app_engine`` の既定に任せ、
    recycle のみ worker 値で override する。SSL も同 factory が接続文字列の
    sslmode から導く。
    """
    pool_size, max_overflow = WORKER_POOL_SIZING[label]
    return create_app_engine(
        settings.database_url,
        application_name=worker_service_name(label),
        echo=False,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_recycle=WORKER_POOL_RECYCLE_SECONDS,
    )


def auth_retention_service_name() -> str:
    """auth schema retention 用 DB 接続の service 名を返す。"""
    return "vector-worker-maintenance-auth"


def build_auth_retention_engine() -> AsyncEngine:
    """auth schema retention 用 engine を作る。

    通常 worker の ``database_url`` は vector_app role で auth."rateLimit" を
    触れないため、auth 保守用の接続文字列を別設定から受ける。
    """
    if settings.auth_retention_database_url is None:
        raise RuntimeError("AUTH_RETENTION_DATABASE_URL is not configured")
    return create_app_engine(
        settings.auth_retention_database_url,
        application_name=auth_retention_service_name(),
        echo=False,
        pool_size=AUTH_RETENTION_POOL_SIZE,
        max_overflow=AUTH_RETENTION_MAX_OVERFLOW,
        pool_recycle=WORKER_POOL_RECYCLE_SECONDS,
    )


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
        state.session_factory = async_sessionmaker(
            state.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
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
                state.auth_session_factory = async_sessionmaker(
                    state.auth_engine,
                    class_=AsyncSession,
                    expire_on_commit=False,
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
    cron 駆動を持つ broker (broker_metadata / broker_trend_discovery /
    broker_briefing / broker_maintenance) のみに本関数を当てる。content / analysis /
    embedding broker は scheduler が存在しないため不要。

    Scheduler 自身は DB を触らない (全 cron task は worker 側で実行され、
    state.engine も session_factory も WORKER_STARTUP でしか初期化されない) ため、
    本 hook は startup/shutdown ログのみを担う (engine 生成 / instrument_sqlalchemy は
    意図的に呼ばない)。

    Logfire bootstrap は本 hook では呼ばない。統合後の scheduler は 1 プロセスで 4
    broker の CLIENT_STARTUP が走るため、hook 内で ``setup_logfire`` を呼ぶと
    ``logfire.instrument_httpx`` (global patch) が 4 回積み重なり「プロセスごとに 1 度」
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


_register_worker_lifecycle(broker_metadata, "metadata")
_register_worker_lifecycle(broker_content, "content")
_register_worker_lifecycle(broker_analysis, "analysis")
_register_worker_lifecycle(broker_embedding, "embedding")
_register_worker_lifecycle(broker_trend_discovery, "trend_discovery")
_register_worker_lifecycle(broker_briefing, "briefing")
_register_worker_lifecycle(broker_maintenance, "maintenance")

# broker_metadata / broker_trend_discovery / broker_briefing / broker_maintenance は
# worker process と scheduler process の両方で同じ broker object を共有するため、
# _register_worker_lifecycle (WORKER_STARTUP) と _register_scheduler_lifecycle
# (CLIENT_STARTUP) の両方を呼ぶ。
# プロセスが違うのでイベント発火が衝突することはない。
_register_scheduler_lifecycle(broker_metadata, "metadata")
_register_scheduler_lifecycle(broker_trend_discovery, "trend_discovery")
_register_scheduler_lifecycle(broker_briefing, "briefing")
_register_scheduler_lifecycle(broker_maintenance, "maintenance")
