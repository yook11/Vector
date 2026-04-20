"""hn_fetch_state (Redis による HN 増分取得 state) のテスト。"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.collection.ingestion.fetchers.hn_fetch_state import (
    get_last_fetched_at,
    set_last_fetched_at,
)

_MODULE = "app.collection.ingestion.fetchers.hn_fetch_state"


@pytest.mark.asyncio
async def test_get_returns_none_when_absent() -> None:
    """未設定なら None を返す。"""
    client = AsyncMock()
    client.get = AsyncMock(return_value=None)
    with patch(f"{_MODULE}.get_redis", return_value=client):
        assert await get_last_fetched_at(42) is None


@pytest.mark.asyncio
async def test_get_parses_iso_string() -> None:
    """Redis に保存された ISO 文字列を datetime に戻す。"""
    ts = datetime(2026, 2, 24, 17, 0, 0, tzinfo=UTC)
    client = AsyncMock()
    client.get = AsyncMock(return_value=ts.isoformat())
    with patch(f"{_MODULE}.get_redis", return_value=client):
        assert await get_last_fetched_at(42) == ts


@pytest.mark.asyncio
async def test_get_swallows_redis_failure() -> None:
    """Redis 障害時は None にフォールバックする (fail-open)。"""
    client = AsyncMock()
    client.get = AsyncMock(side_effect=ConnectionError("redis down"))
    with patch(f"{_MODULE}.get_redis", return_value=client):
        assert await get_last_fetched_at(42) is None


@pytest.mark.asyncio
async def test_set_writes_iso_string() -> None:
    """保存時は ISO 文字列として書き込む。"""
    ts = datetime(2026, 2, 24, 17, 0, 0, tzinfo=UTC)
    client = AsyncMock()
    client.set = AsyncMock()
    with patch(f"{_MODULE}.get_redis", return_value=client):
        await set_last_fetched_at(42, ts)
    client.set.assert_awaited_once_with("hn_fetch_state:42", ts.isoformat())


@pytest.mark.asyncio
async def test_set_swallows_redis_failure() -> None:
    """Redis 障害時は例外を飲み込む (fire-and-forget)。"""
    ts = datetime(2026, 2, 24, 17, 0, 0, tzinfo=UTC)
    client = AsyncMock()
    client.set = AsyncMock(side_effect=ConnectionError("redis down"))
    with patch(f"{_MODULE}.get_redis", return_value=client):
        await set_last_fetched_at(42, ts)
