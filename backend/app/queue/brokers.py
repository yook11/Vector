"""taskiq broker 定義。

broker:
  - broker_dispatch:   dispatch / sweep control task
  - broker_collection: acquisition / completion の2 Stream共有 consumer
  - broker_analysis:  AI 分析
  - broker_embedding: ベクトル埋め込み生成
  - broker_briefing:  週次カテゴリ別 LLM ブリーフィング生成 (cron 駆動、別 queue)
  - broker_agent:     user-facing research agent 非同期 run + deadline sweeper
  - broker_maintenance: back-fill 救済 + retention purge + queue-health 観測の
    core 系保守 cron (cron 駆動、collect から分離するため別 queue)

Workers: broker ごとに 1 つ (docker-compose.yml / supervisord conf を参照)。
Scheduler / lifecycle の attach は本 module の **末尾の副作用 import** で行う。
`from app.queue.brokers import broker_X` 1 行で:
  - broker × 7 の生成
  - 各 broker への WORKER_STARTUP / CLIENT_STARTUP hook attach
  - 共通catalogに残る4つのTaskiqSchedulerの生成
が全て完了する。AI adapter 配線は lifecycle の WorkerRuntime.compose が呼ぶ。
順序は循環 import を避けるため厳守。
"""

from __future__ import annotations

from taskiq_redis import RedisStreamBroker

from app.config import settings
from app.redis.clients import taskiq_stream_connection
from app.redis.taskiq_stream_broker import create_taskiq_stream_broker

# IAM 認証時は URL の user が credential_provider へ移る (taskiq-redis は
# **connection_kwargs を redis-py の ConnectionPool まで素通しする)。
_stream = taskiq_stream_connection(settings)


def _make_broker(
    queue_name: str,
    *,
    additional_streams: dict[str, str | int] | None = None,
    consumer_group_name: str = "taskiq",
    consumer_id: str = "$",
    unacknowledged_batch_size: int = 100,
    unacknowledged_lock_timeout: float | None = None,
) -> RedisStreamBroker:
    return create_taskiq_stream_broker(
        _stream,
        queue_name,
        additional_streams=additional_streams,
        consumer_group_name=consumer_group_name,
        consumer_id=consumer_id,
        unacknowledged_batch_size=unacknowledged_batch_size,
        unacknowledged_lock_timeout=unacknowledged_lock_timeout,
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
broker_briefing = _make_broker("briefing")
broker_agent = _make_broker("agent")
broker_maintenance = _make_broker("pipeline:maintenance")


# broker object が出揃ったあとで lifecycle / schedulers を attach する。
# 各 module は import するだけで broker.on_event() に hook を登録する副作用
# を持つ。本 module の末尾に置くことで:
#   - broker × 7 が定義済の状態で各 hook 登録が走る
#   - `from app.queue.brokers import broker_X` 単独で lifecycle 完了が保証される
#     (test や entrypoint が個別に lifecycle module を import する必要なし)
import app.queue.lifecycle  # noqa: E402, F401, I001
import app.queue.schedulers  # noqa: E402, F401, I001
