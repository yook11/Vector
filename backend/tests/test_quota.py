"""日次クォータ (check_daily_quota) のテスト。"""

from unittest.mock import AsyncMock, patch

import pytest

_MOD = "app.collection.ingestion.quota"


@pytest.mark.asyncio
async def test_allows_when_under_limit() -> None:
    """カウントが上限未満のとき True を返す。"""
    mock_redis = AsyncMock()
    mock_redis.incr = AsyncMock(return_value=10)
    mock_redis.expire = AsyncMock()

    with patch(f"{_MOD}.get_redis", return_value=mock_redis):
        from app.collection.ingestion.quota import check_daily_quota

        result = await check_daily_quota(source_id=1, limit=25)

    assert result is True


@pytest.mark.asyncio
async def test_blocks_when_over_limit() -> None:
    """カウントが上限超過のとき False を返す。"""
    mock_redis = AsyncMock()
    mock_redis.incr = AsyncMock(return_value=26)
    mock_redis.expire = AsyncMock()

    with patch(f"{_MOD}.get_redis", return_value=mock_redis):
        from app.collection.ingestion.quota import check_daily_quota

        result = await check_daily_quota(source_id=1, limit=25)

    assert result is False


@pytest.mark.asyncio
async def test_allows_at_exact_limit() -> None:
    """カウントが上限ちょうどのとき True を返す（limit 回目は許可）。"""
    mock_redis = AsyncMock()
    mock_redis.incr = AsyncMock(return_value=25)
    mock_redis.expire = AsyncMock()

    with patch(f"{_MOD}.get_redis", return_value=mock_redis):
        from app.collection.ingestion.quota import check_daily_quota

        result = await check_daily_quota(source_id=1, limit=25)

    assert result is True


@pytest.mark.asyncio
async def test_fails_open_on_redis_error() -> None:
    """Redis 障害時は True を返す（fail-open）。"""
    mock_redis = AsyncMock()
    mock_redis.incr = AsyncMock(side_effect=ConnectionError("Redis down"))

    with patch(f"{_MOD}.get_redis", return_value=mock_redis):
        from app.collection.ingestion.quota import check_daily_quota

        result = await check_daily_quota(source_id=1, limit=25)

    assert result is True


@pytest.mark.asyncio
async def test_key_includes_source_id_and_date() -> None:
    """キーにソース ID と日付が含まれる。"""
    from datetime import date

    mock_redis = AsyncMock()
    mock_redis.incr = AsyncMock(return_value=1)
    mock_redis.expire = AsyncMock()

    with patch(f"{_MOD}.get_redis", return_value=mock_redis):
        from app.collection.ingestion.quota import check_daily_quota

        await check_daily_quota(source_id=42, limit=25)

    expected_key = f"source_quota:42:{date.today().isoformat()}"
    mock_redis.incr.assert_called_once_with(expected_key)
