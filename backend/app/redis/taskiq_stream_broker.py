"""Redis Streamを使うTaskiq brokerの共通factory。"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from redis.asyncio import Redis
from taskiq import AckableMessage, AsyncBroker, SimpleRetryMiddleware

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

    async def listen(self) -> AsyncGenerator[AckableMessage]:
        """新着配送がない場合も、期限を過ぎた未ACK配送の回収へ進む。"""
        async with Redis(connection_pool=self.connection_pool) as redis:
            while True:
                new_deliveries = await redis.xreadgroup(
                    groupname=self.consumer_group_name,
                    consumername=self.consumer_name,
                    streams={self.queue_name: ">", **self.additional_streams},
                    block=self.block,
                    count=self.count,
                    noack=False,
                )
                for stream, deliveries in new_deliveries or []:
                    for message_id, message in deliveries:
                        yield AckableMessage(
                            data=message[b"data"],
                            ack=self._ack_generator(id=message_id, queue_name=stream),
                        )

                # 新着がない場合も回収へ進み、taskiq-redisと同じ排他・ACK契約を保つ。
                for stream in [self.queue_name, *self.additional_streams.keys()]:
                    async with redis.pipeline() as pipeline:
                        lock = pipeline.lock(
                            f"autoclaim:{self.consumer_group_name}:{stream}",
                            timeout=self.unacknowledged_lock_timeout,
                        )
                        await lock.acquire()
                        await pipeline.xautoclaim(
                            name=stream,
                            groupname=self.consumer_group_name,
                            consumername=self.consumer_name,
                            min_idle_time=self.idle_timeout,
                            count=self.unacknowledged_batch_size,
                        )
                        await lock.release()
                        results = await pipeline.execute()
                    recovered_deliveries = results[1][1]
                    for message_id, message in recovered_deliveries:
                        yield AckableMessage(
                            data=message[b"data"],
                            ack=self._ack_generator(id=message_id, queue_name=stream),
                        )

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
