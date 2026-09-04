"""用途別の Redis client 生成入口と共通の組み立て規則。"""

from __future__ import annotations

from typing import Any, NamedTuple, Protocol

import redis.asyncio as aioredis

from app.redis.iam_auth import redis_connection_options

AGENT_LIVE_API_MAX_CONNECTIONS = 64
AGENT_LIVE_WORKER_MAX_CONNECTIONS = 16
AGENT_LIVE_SOCKET_TIMEOUT_SECONDS = 2
AGENT_LIVE_SOCKET_CONNECT_TIMEOUT_SECONDS = 5

PIPELINE_CONTROL_MAX_CONNECTIONS = 12
PIPELINE_CONTROL_CLI_MAX_CONNECTIONS = 4
PIPELINE_CONTROL_SOCKET_TIMEOUT_SECONDS = 5
PIPELINE_CONTROL_SOCKET_CONNECT_TIMEOUT_SECONDS = 5

TASKIQ_STREAM_MAX_CONNECTIONS = 8
# XREADGROUP BLOCK より短くすると listener が落ちる。
TASKIQ_STREAM_SOCKET_TIMEOUT_SECONDS = 30
TASKIQ_STREAM_SOCKET_CONNECT_TIMEOUT_SECONDS = 5


class _RuntimeRedisSettings(Protocol):
    """用途別 client 生成に必要な設定。"""

    redis_url: str
    redis_iam_auth: bool
    aws_region: str | None
    redis_iam_cache_name: str | None


class TaskiqStreamConnection(NamedTuple):
    """Taskiq Stream broker が加工せず渡せる接続指定。"""

    url: str
    max_connection_pool_size: int
    connection_kwargs: dict[str, Any]


def _connection_options(
    settings: _RuntimeRedisSettings,
) -> tuple[str, dict[str, Any]]:
    """runtime settings を設定非依存の接続 builder へ展開する。"""
    return redis_connection_options(
        settings.redis_url,
        iam_auth=settings.redis_iam_auth,
        region=settings.aws_region,
        cache_name=settings.redis_iam_cache_name,
    )


def _create_client(
    url: str,
    *,
    iam_kwargs: dict[str, Any],
    max_connections: int,
    socket_timeout: float,
    socket_connect_timeout: float,
) -> aioredis.Redis:
    """IAM と用途別 pool / timeout を載せて client を組み立てる。"""
    return aioredis.from_url(
        url,
        decode_responses=True,
        max_connections=max_connections,
        socket_timeout=socket_timeout,
        socket_connect_timeout=socket_connect_timeout,
        **iam_kwargs,
    )


def _create_agent_live_client(
    settings: _RuntimeRedisSettings, *, max_connections: int
) -> aioredis.Redis:
    url, iam_kwargs = _connection_options(settings)
    return _create_client(
        url,
        iam_kwargs=iam_kwargs,
        max_connections=max_connections,
        socket_timeout=AGENT_LIVE_SOCKET_TIMEOUT_SECONDS,
        socket_connect_timeout=AGENT_LIVE_SOCKET_CONNECT_TIMEOUT_SECONDS,
    )


def _create_pipeline_control_client(
    settings: _RuntimeRedisSettings, *, max_connections: int
) -> aioredis.Redis:
    url, iam_kwargs = _connection_options(settings)
    return _create_client(
        url,
        iam_kwargs=iam_kwargs,
        max_connections=max_connections,
        socket_timeout=PIPELINE_CONTROL_SOCKET_TIMEOUT_SECONDS,
        socket_connect_timeout=PIPELINE_CONTROL_SOCKET_CONNECT_TIMEOUT_SECONDS,
    )


def create_api_agent_live_client(settings: _RuntimeRedisSettings) -> aioredis.Redis:
    """API プロセスが所有する agent live client を作る。"""
    return _create_agent_live_client(
        settings, max_connections=AGENT_LIVE_API_MAX_CONNECTIONS
    )


def create_worker_agent_live_client(settings: _RuntimeRedisSettings) -> aioredis.Redis:
    """agent worker が所有する agent live client を作る。"""
    return _create_agent_live_client(
        settings, max_connections=AGENT_LIVE_WORKER_MAX_CONNECTIONS
    )


def create_worker_pipeline_control_client(
    settings: _RuntimeRedisSettings,
) -> aioredis.Redis:
    """analysis / embedding / maintenance worker が所有する control client を作る。"""
    return _create_pipeline_control_client(
        settings, max_connections=PIPELINE_CONTROL_MAX_CONNECTIONS
    )


def create_cli_pipeline_control_client(
    settings: _RuntimeRedisSettings,
) -> aioredis.Redis:
    """短命 CLI が使う control client を作る。"""
    return _create_pipeline_control_client(
        settings, max_connections=PIPELINE_CONTROL_CLI_MAX_CONNECTIONS
    )


def taskiq_stream_connection(
    settings: _RuntimeRedisSettings,
) -> TaskiqStreamConnection:
    """Taskiq Stream broker へ渡す接続指定を返す。

    decode_responses は載せない。符号化は Taskiq が持つ。
    """
    url, iam_kwargs = _connection_options(settings)
    return TaskiqStreamConnection(
        url=url,
        max_connection_pool_size=TASKIQ_STREAM_MAX_CONNECTIONS,
        connection_kwargs={
            **iam_kwargs,
            "socket_timeout": TASKIQ_STREAM_SOCKET_TIMEOUT_SECONDS,
            "socket_connect_timeout": TASKIQ_STREAM_SOCKET_CONNECT_TIMEOUT_SECONDS,
        },
    )
