"""パイプライン用タスクキューの broker 定義と共通基盤。

broker:
  - broker_metadata:  RSS/HN メタデータ取得 + dispatch
  - broker_content:   記事単位のコンテンツ抽出
  - broker_analysis:  AI 分析
  - broker_embedding: ベクトル埋め込み生成
  - broker_digest:    週次トレンド snapshot 生成 (cron 駆動)
  - broker_briefing:  週次カテゴリ別 LLM ブリーフィング生成 (cron 駆動、別 queue)

Workers: broker ごとに 1 つ（docker-compose.yml を参照）。
Scheduler:
  - taskiq scheduler app.brokers:scheduler_metadata (back-fill 用 cron)
  - taskiq scheduler app.brokers:scheduler_digest (週次 snapshot 用 cron)
  - taskiq scheduler app.brokers:scheduler_briefing (週次 briefing 用 cron)
"""

from __future__ import annotations

import logfire
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

# taskiq 0.12.4: taskiq.middlewares.__init__ には未公開のためサブモジュール直 import
# が正規 (re-export がない)。version up で公開された場合は `from taskiq.middlewares
# import OpenTelemetryMiddleware` に切替可。
from taskiq.middlewares.opentelemetry_middleware import OpenTelemetryMiddleware
from taskiq.schedule_sources import LabelScheduleSource
from taskiq_redis import RedisAsyncResultBackend, RedisStreamBroker

from app.analysis.assessment.ai.deepseek import DeepSeekAssessor
from app.analysis.curation.ai.gemini import GeminiCurator
from app.analysis.embedding.ai.gemini import GeminiEmbedder
from app.analysis.rate_limit import ProviderRateLimitGate
from app.collection.sources.fetch_cadence import FetchCadence
from app.config import settings
from app.logfire_setup import setup_logfire

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Fetch cadence tier → cron 写像
# ---------------------------------------------------------------------------
# tier 別に固定間隔で dispatch する。間隔はコード固定 (cadence spec §2)。
# 調整は本 dict の変更 + scheduler restart のみで可逆 (env / DB を経由しない)。

CADENCE_CRON: dict[FetchCadence, str] = {
    FetchCadence.HIGH: "*/15 * * * *",  # 15 分
    FetchCadence.MEDIUM: "0 * * * *",  # 1 時間
    FetchCadence.LOW: "0 */6 * * *",  # 6 時間
}

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
        # OTel middleware を **最初** に挿す。pre_execute は登録順 (FIFO) ・
        # post_execute / post_save は逆順 (LIFO) のため、これで consumer span が
        # SimpleRetry の判定より外側に open/close する (1 execute サイクル内の
        # handler 例外は span 範囲に含まれる)。tracer / meter provider 引数なしで
        # logfire.configure() が立てた OTel global Proxy provider に遅延束縛される
        # (configure は WORKER_STARTUP / CLIENT_STARTUP の中、middleware __init__
        # は本ファイル import 時で先行するが、Proxy{Tracer,Meter}Provider が後付け
        # 実 provider に委譲する設計のため成立する; 本契約は
        # tests/test_brokers_otel_middleware.py の 4-3 capfire oracle で pin)。
        #
        # 注: SimpleRetry の retry 経路は新規 enqueue (broker.kick) で実装されて
        # おり、retry が発火する execute は別 trace_id を持つ (現状
        # default_retry_count=0 で発火しないため実害ゼロ)。retry を有効化する
        # 場合は別 spec で trace 連結戦略を定める。
        .with_middlewares(
            OpenTelemetryMiddleware(),
            SimpleRetryMiddleware(default_retry_count=0),
        )
    )


broker_metadata = _make_broker("pipeline:metadata")
broker_content = _make_broker("pipeline:content")
broker_analysis = _make_broker("pipeline:analysis")
broker_embedding = _make_broker("pipeline:embedding")
broker_digest = _make_broker("digest")
broker_briefing = _make_broker("briefing")

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
scheduler_briefing = TaskiqScheduler(
    broker=broker_briefing,
    sources=[LabelScheduleSource(broker_briefing)],
)

# ---------------------------------------------------------------------------
# ライフサイクルフック — broker ごとに独自の engine を持つ
# ---------------------------------------------------------------------------


def _register_lifecycle(broker: RedisStreamBroker, label: str) -> None:
    @broker.on_event(TaskiqEvents.WORKER_STARTUP)
    async def on_startup(state: TaskiqState) -> None:
        # 可観測性 bootstrap は engine 生成や追加 startup hook
        # (_wire_*_adapters) より先に走らせ、それらのログも structlog →
        # Logfire 経路に乗るようにする。各 worker プロセスでは自分の broker の
        # on_startup だけが発火するため、プロセスごとに正しい service_name で
        # 1 回ずつ呼ばれる。
        setup_logfire(f"vector-worker-{label}")
        state.engine = create_async_engine(settings.database_url, echo=False)
        state.session_factory = async_sessionmaker(
            state.engine,
            class_=SQLModelAsyncSession,
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


_register_lifecycle(broker_metadata, "metadata")
_register_lifecycle(broker_content, "content")
_register_lifecycle(broker_analysis, "analysis")
_register_lifecycle(broker_embedding, "embedding")
_register_lifecycle(broker_digest, "digest")
_register_lifecycle(broker_briefing, "briefing")


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


# broker_metadata / broker_digest / broker_briefing は worker process と scheduler
# process の両方で同じ broker object を共有するため、_register_lifecycle
# (WORKER_STARTUP) と _register_scheduler_lifecycle (CLIENT_STARTUP) の両方を呼ぶ。
# プロセスが違うのでイベント発火が衝突することはない。
_register_scheduler_lifecycle(broker_metadata, "metadata")
_register_scheduler_lifecycle(broker_digest, "digest")
_register_scheduler_lifecycle(broker_briefing, "briefing")

# ---------------------------------------------------------------------------
# AI アダプター wiring — broker_analysis 専用 composition root
# ---------------------------------------------------------------------------
# Provider 選択は本ファイルで hardcode する設計（Pure DI）。切替は env 変更
# ではなくコード変更 + worker restart。Stage 1 と Stage 2 で別の抽象を別の
# クラスに紐付けるため、共有 env による誤切替の余地が構造的に生じない。


@broker_analysis.on_event(TaskiqEvents.WORKER_STARTUP)
async def _wire_analysis_adapters(state: TaskiqState) -> None:
    """Stage 3 / Stage 4 の AI アダプターを worker 起動時に構築する。"""
    state.curator = GeminiCurator()
    state.assessor = DeepSeekAssessor()
    state.provider_rate_limit_gate = ProviderRateLimitGate()
    logger.info(
        "analysis_adapters_wired",
        curator=type(state.curator).__name__,
        curator_model=state.curator.model_name,
        assessor=type(state.assessor).__name__,
        assessor_model=state.assessor.model_name,
    )


@broker_embedding.on_event(TaskiqEvents.WORKER_STARTUP)
async def _wire_embedding_adapters(state: TaskiqState) -> None:
    """Stage 5 の embedder アダプターを worker 起動時に構築する。"""
    state.embedder = GeminiEmbedder()
    state.provider_rate_limit_gate = ProviderRateLimitGate()
    logger.info(
        "embedding_adapters_wired",
        embedder=type(state.embedder).__name__,
        embedder_model=state.embedder.model_name,
    )


# ---------------------------------------------------------------------------
# ヘルパー（タスクモジュール間で共有）
# ---------------------------------------------------------------------------
# 副作用 import より先に定義する: cron 駆動の task モジュール (briefing 等) が
# トップレベルで `from app.brokers import is_last_attempt` するため、循環 import
# (本モジュール実行中に side-effect import で呼び戻され、まだ未定義) を避ける。


def is_last_attempt(ctx: Context) -> bool:
    """この試行後に SimpleRetryMiddleware がリトライしない場合 True を返す。"""
    labels = ctx.message.labels
    retry_count = int(labels.get("retry_count", 0))
    max_retries = int(labels.get("max_retries", 0))
    return retry_count >= max_retries


# scheduler に cron を登録するため、import で副作用を起こす。
import app.audit.retention  # noqa: E402, F401
import app.collection.article_completion.dispatch  # noqa: E402, F401
import app.insights.briefing.tasks.briefing  # noqa: E402, F401
import app.insights.snapshot.tasks.snapshot  # noqa: E402, F401
import app.maintenance.tasks  # noqa: E402, F401
