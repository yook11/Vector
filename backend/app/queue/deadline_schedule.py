"""run期限確認の単発予約と、その接続の所有。"""

from datetime import datetime

from redis.asyncio import Redis
from taskiq import ScheduledTask
from taskiq.compat import model_dump
from taskiq.serializers import JSONSerializer
from taskiq_redis import ListRedisScheduleSource

from app.config import Settings
from app.redis.clients import taskiq_stream_connection

DEADLINE_SCHEDULE_PREFIX = "vector-agent-deadline"


def _encode_datetime(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError("unsupported deadline schedule value")


class AgentDeadlineScheduleSource(ListRedisScheduleSource):
    async def add_schedule(self, schedule: ScheduledTask) -> None:
        if (
            schedule.time is None
            or schedule.cron is not None
            or schedule.interval is not None
        ):
            raise ValueError("deadline checks require a one-time schedule")
        async with Redis(connection_pool=self._connection_pool) as redis:
            # 本体と時刻索引は、予約途中の停止でも片方だけを残さない。
            async with redis.pipeline(transaction=True) as pipeline:
                pipeline.set(
                    self._get_data_key(schedule.schedule_id),
                    self._serializer.dumpb(model_dump(schedule)),
                )
                pipeline.rpush(self._get_time_key(schedule.time), schedule.schedule_id)
                await pipeline.execute()

    async def shutdown(self) -> None:
        await self._connection_pool.disconnect()


def create_deadline_schedule_source(settings: Settings) -> AgentDeadlineScheduleSource:
    connection = taskiq_stream_connection(settings)
    return AgentDeadlineScheduleSource(
        connection.url,
        prefix=DEADLINE_SCHEDULE_PREFIX,
        max_connection_pool_size=connection.max_connection_pool_size,
        serializer=JSONSerializer(default=_encode_datetime),
        skip_past_schedules=False,
        **(
            connection.connection_kwargs
            | {"socket_timeout": 2, "socket_connect_timeout": 2, "timeout": 2}
        ),
    )
