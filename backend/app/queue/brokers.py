"""taskiq broker 定義。

broker:
  - broker_dispatch:   dispatch / sweep control task
  - broker_collection: acquisition / completion の2 Stream共有 consumer
  - broker_analysis:  AI 分析
  - broker_embedding: ベクトル埋め込み生成
  - broker_trend_discovery: rolling 7d Trend Discovery 実行 (cron 駆動)
  - broker_briefing:  週次カテゴリ別 LLM ブリーフィング生成 (cron 駆動、別 queue)
  - broker_agent:     user-facing research agent 非同期 run + deadline sweeper
  - broker_maintenance: back-fill 救済 + retention purge + queue-health 観測の
    core 系保守 cron (cron 駆動、collect から分離するため別 queue)

Workers: broker ごとに 1 つ (docker-compose.yml / supervisord conf を参照)。
Scheduler / lifecycle の attach は本 module の **末尾の副作用 import** で行う。
`from app.queue.brokers import broker_X` 1 行で:
  - broker × 8 の生成
  - 各 broker への WORKER_STARTUP / CLIENT_STARTUP hook attach
  - 5 つの TaskiqScheduler の生成
が全て完了する。AI adapter 配線は lifecycle の WorkerRuntime.compose が呼ぶ。
順序は循環 import を避けるため厳守。
"""

from __future__ import annotations

import structlog
from taskiq import AsyncBroker, SimpleRetryMiddleware

# taskiq 0.12.4: taskiq.middlewares.__init__ には未公開のためサブモジュール直 import
# が正規 (re-export がない)。version up で公開された場合は `from taskiq.middlewares
# import OpenTelemetryMiddleware` に切替可。
from taskiq.middlewares.opentelemetry_middleware import OpenTelemetryMiddleware
from taskiq_redis import RedisStreamBroker

from app.config import settings
from app.redis.clients import taskiq_stream_connection

logger = structlog.get_logger(__name__)

# IAM 認証時は URL の user が credential_provider へ移る (taskiq-redis は
# **connection_kwargs を redis-py の ConnectionPool まで素通しする)。
_stream = taskiq_stream_connection(settings)


class _RedisStreamBroker(RedisStreamBroker):
    """consumer group は worker / scheduler だけが宣言する。"""

    async def startup(self) -> None:
        await AsyncBroker.startup(self)
        if self.is_worker_process or self.is_scheduler_process:
            await self._declare_consumer_group()

    async def shutdown(self) -> None:
        try:
            await AsyncBroker.shutdown(self)
        finally:
            await self.connection_pool.disconnect()


def _make_broker(
    queue_name: str,
    *,
    additional_streams: dict[str, str | int] | None = None,
    consumer_group_name: str = "taskiq",
    consumer_id: str = "$",
    unacknowledged_batch_size: int = 100,
    unacknowledged_lock_timeout: float | None = None,
) -> RedisStreamBroker:
    return (
        _RedisStreamBroker(
            url=_stream.url,
            idle_timeout=600_000,
            maxlen=10_000,
            queue_name=queue_name,
            additional_streams=additional_streams,
            consumer_group_name=consumer_group_name,
            consumer_id=consumer_id,
            unacknowledged_batch_size=unacknowledged_batch_size,
            unacknowledged_lock_timeout=unacknowledged_lock_timeout,
            max_connection_pool_size=_stream.max_connection_pool_size,
            **_stream.connection_kwargs,
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


broker_dispatch = _make_broker("pipeline:dispatch")
broker_collection = _make_broker(
    "pipeline:acquisition",
    additional_streams={"pipeline:completion": ">"},
    consumer_id="0-0",
    unacknowledged_lock_timeout=60,
)
broker_analysis = _make_broker(
    "pipeline:curation",
    additional_streams={"pipeline:assessment": ">"},
    consumer_id="0-0",
    unacknowledged_lock_timeout=60,
)
broker_embedding = _make_broker("pipeline:embedding")
broker_trend_discovery = _make_broker("trend_discovery")
broker_briefing = _make_broker("briefing")
broker_agent = _make_broker("agent")
broker_maintenance = _make_broker("pipeline:maintenance")


# broker object が出揃ったあとで lifecycle / schedulers を attach する。
# 各 module は import するだけで broker.on_event() に hook を登録する副作用
# を持つ。本 module の末尾に置くことで:
#   - broker × 8 が定義済の状態で各 hook 登録が走る
#   - `from app.queue.brokers import broker_X` 単独で lifecycle 完了が保証される
#     (test や entrypoint が個別に lifecycle module を import する必要なし)
import app.queue.lifecycle  # noqa: E402, F401, I001
import app.queue.schedulers  # noqa: E402, F401, I001
