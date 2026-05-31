"""taskiq broker 定義。

broker:
  - broker_metadata:  RSS/HN メタデータ取得 + dispatch
  - broker_content:   記事単位のコンテンツ抽出
  - broker_analysis:  AI 分析
  - broker_embedding: ベクトル埋め込み生成
  - broker_trend_discovery: rolling 7d Trend Discovery 実行 (cron 駆動)
  - broker_briefing:  週次カテゴリ別 LLM ブリーフィング生成 (cron 駆動、別 queue)
  - broker_maintenance: back-fill 救済 + retention purge の core 系保守 cron
    (cron 駆動、collect から分離するため別 queue)

Workers: broker ごとに 1 つ (docker-compose.yml / supervisord conf を参照)。
Scheduler / lifecycle / AI composition の attach は本 module の **末尾の副作用
import** で行う。`from app.queue.brokers import broker_X` 1 行で:
  - broker × 7 の生成
  - 各 broker への WORKER_STARTUP / CLIENT_STARTUP hook attach
  - analysis / embedding broker への AI adapter wiring attach
  - 3 つの TaskiqScheduler の生成
が全て完了する。順序は循環 import を避けるため厳守。
"""

from __future__ import annotations

import structlog
from taskiq import SimpleRetryMiddleware

# taskiq 0.12.4: taskiq.middlewares.__init__ には未公開のためサブモジュール直 import
# が正規 (re-export がない)。version up で公開された場合は `from taskiq.middlewares
# import OpenTelemetryMiddleware` に切替可。
from taskiq.middlewares.opentelemetry_middleware import OpenTelemetryMiddleware
from taskiq_redis import RedisAsyncResultBackend, RedisStreamBroker

from app.config import settings

logger = structlog.get_logger(__name__)


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
                # result key を taskiq:<task_id> に名前空間化する。prefix_str 無しだと
                # bare uuid となり Redis ACL で stream key と区別できない。collect の
                # ~taskiq:* grant を実在させ set_result の NOPERM を防ぐ
                # (infra/redis/fly.toml の ACL と対)。
                prefix_str="taskiq",
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
broker_trend_discovery = _make_broker("trend_discovery")
broker_briefing = _make_broker("briefing")
broker_maintenance = _make_broker("pipeline:maintenance")


# broker object が出揃ったあとで lifecycle / composition / schedulers を attach
# する。各 module は import するだけで broker.on_event() に hook を登録する副作用
# を持つ。本 module の末尾に置くことで:
#   - broker × 7 が定義済の状態で各 hook 登録が走る
#   - `from app.queue.brokers import broker_X` 単独で lifecycle 完了が保証される
#     (test や entrypoint が個別に lifecycle module を import する必要なし)
import app.queue.composition  # noqa: E402, F401
import app.queue.lifecycle  # noqa: E402, F401
import app.queue.schedulers  # noqa: E402, F401
