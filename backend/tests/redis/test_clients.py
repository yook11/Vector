"""用途別 Redis factory の組み立て契約。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.redis.clients import (
    create_api_agent_live_client,
    create_cli_pipeline_control_client,
    create_worker_agent_live_client,
    create_worker_pipeline_control_client,
    taskiq_stream_connection,
)
from app.redis.iam_auth import ElastiCacheIAMProvider

_REGION = "ap-northeast-1"
_CACHE_NAME = "vector-cache-abc123"
_IAM_URL = "redis://vector-app@vector-cache.abc.cache.amazonaws.com:6379/3"


class _RedisSettings:
    def __init__(self, redis_url: str) -> None:
        self.redis_url = redis_url


def _enable_iam(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "redis_iam_auth", True)
    monkeypatch.setattr(settings, "aws_region", _REGION)
    monkeypatch.setattr(settings, "redis_iam_cache_name", _CACHE_NAME)


def test_create_api_agent_live_client_uses_api_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = MagicMock()
    monkeypatch.setattr("app.redis.clients.aioredis.from_url", spy)
    create_api_agent_live_client(settings)
    assert spy.call_args.kwargs["max_connections"] == 64


def test_create_worker_agent_live_client_uses_worker_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = MagicMock()
    monkeypatch.setattr("app.redis.clients.aioredis.from_url", spy)
    create_worker_agent_live_client(settings)
    assert spy.call_args.kwargs["max_connections"] == 16


def test_create_worker_pipeline_control_client_uses_worker_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = MagicMock()
    monkeypatch.setattr("app.redis.clients.aioredis.from_url", spy)
    create_worker_pipeline_control_client(settings)
    assert spy.call_args.kwargs["max_connections"] == 12


def test_create_cli_pipeline_control_client_uses_cli_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = MagicMock()
    monkeypatch.setattr("app.redis.clients.aioredis.from_url", spy)
    create_cli_pipeline_control_client(settings)
    assert spy.call_args.kwargs["max_connections"] == 4


def test_create_api_agent_live_client_keeps_iam_credential_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_iam(monkeypatch)
    spy = MagicMock()
    monkeypatch.setattr("app.redis.clients.aioredis.from_url", spy)
    create_api_agent_live_client(_RedisSettings(_IAM_URL))
    provider = spy.call_args.kwargs["credential_provider"]
    assert isinstance(provider, ElastiCacheIAMProvider)


def test_taskiq_stream_connection_keeps_iam_credential_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_iam(monkeypatch)
    connection = taskiq_stream_connection(_RedisSettings(_IAM_URL))
    assert isinstance(
        connection.connection_kwargs["credential_provider"],
        ElastiCacheIAMProvider,
    )


def test_taskiq_stream_connection_is_ready_for_taskiq() -> None:
    connection = taskiq_stream_connection(settings)
    assert connection.max_connection_pool_size == 8
    assert "decode_responses" not in connection.connection_kwargs
    assert "max_connections" not in connection.connection_kwargs
    assert connection.connection_kwargs["socket_timeout"] == 30
    assert connection.connection_kwargs["socket_connect_timeout"] == 5
