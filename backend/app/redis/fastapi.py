"""Redis client を FastAPI の依存性注入へ載せるアダプター。"""

import redis.asyncio as aioredis
from fastapi import Request


def get_agent_live_redis(request: Request) -> aioredis.Redis:
    """API が所有する agent live client を返す。"""
    return request.app.state.agent_live_redis
