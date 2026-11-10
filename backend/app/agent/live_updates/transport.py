"""API プロセスが所有する live Redis を cancel / SSE に渡す。"""

from __future__ import annotations

from uuid import UUID

import redis.asyncio as aioredis
from fastapi import Request

from app.agent.live_updates.stream import (
    AgentRunLiveStreamPublisher,
    AgentRunLiveStreamReader,
    agent_run_live_stream_key,
)
from app.agent.running.types import RunAttempt


class AgentLiveTransport:
    def __init__(self, redis: aioredis.Redis) -> None:
        self._redis = redis

    def reader(self) -> AgentRunLiveStreamReader:
        return AgentRunLiveStreamReader(self._redis)

    def publisher(self, attempt: RunAttempt) -> AgentRunLiveStreamPublisher:
        return AgentRunLiveStreamPublisher(self._redis, attempt)

    async def exists(self, run_id: UUID) -> bool:
        return bool(await self._redis.exists(agent_run_live_stream_key(run_id)))


def get_agent_live_transport(request: Request) -> AgentLiveTransport:
    return request.app.state.agent_live_transport
