"""Redis infra primitives."""

from app.redis.connection import get_redis

__all__ = ["get_redis"]
