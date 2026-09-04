"""Redis Streamを使うTaskiq brokerの共通factory。"""

from __future__ import annotations

from taskiq import AsyncBroker, SimpleRetryMiddleware

# taskiq 0.12.4ではtaskiq.middlewaresから公開されていない。
from taskiq.middlewares.opentelemetry_middleware import OpenTelemetryMiddleware
from taskiq_redis import RedisStreamBroker

from app.redis.clients import TaskiqStreamConnection


class _RedisStreamBroker(RedisStreamBroker):
    """consumer groupはworkerとschedulerだけが宣言する。"""

    async def startup(self) -> None:
        await AsyncBroker.startup(self)
        if self.is_worker_process or self.is_scheduler_process:
            await self._declare_consumer_group()

    async def shutdown(self) -> None:
        try:
            await AsyncBroker.shutdown(self)
        finally:
            await self.connection_pool.disconnect()


def create_taskiq_stream_broker(
    connection: TaskiqStreamConnection,
    queue_name: str,
    *,
    additional_streams: dict[str, str | int] | None = None,
    consumer_group_name: str = "taskiq",
    consumer_id: str = "$",
    unacknowledged_batch_size: int = 100,
    unacknowledged_lock_timeout: float | None = None,
) -> RedisStreamBroker:
    """共通の接続・Stream・middleware契約でTaskiq brokerを作る。"""
    broker = _RedisStreamBroker(
        url=connection.url,
        idle_timeout=600_000,
        maxlen=10_000,
        queue_name=queue_name,
        additional_streams=additional_streams,
        consumer_group_name=consumer_group_name,
        consumer_id=consumer_id,
        unacknowledged_batch_size=unacknowledged_batch_size,
        unacknowledged_lock_timeout=unacknowledged_lock_timeout,
        max_connection_pool_size=connection.max_connection_pool_size,
        **connection.connection_kwargs,
    )
    # OTelを先頭に置き、consumer spanがretry判定を包含する順序を保つ。
    return broker.with_middlewares(
        OpenTelemetryMiddleware(),
        SimpleRetryMiddleware(default_retry_count=0),
    )
