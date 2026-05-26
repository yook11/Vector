"""consume_daily_budget のユニットテスト (Lua eval を MagicMock)。"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.queue.helpers.budget import consume_daily_budget


@pytest.mark.asyncio
async def test_returns_granted_when_below_max() -> None:
    """Lua スクリプトが許可数を返したら関数も同じ値を返す。"""
    redis = MagicMock()
    redis.eval = AsyncMock(return_value=10)
    granted = await consume_daily_budget(redis, "extract", 10, 600)
    assert granted == 10


@pytest.mark.asyncio
async def test_returns_zero_when_exhausted() -> None:
    """既に当日 daily_max に達していれば 0 を返す。"""
    redis = MagicMock()
    redis.eval = AsyncMock(return_value=0)
    granted = await consume_daily_budget(redis, "extract", 50, 600)
    assert granted == 0


@pytest.mark.asyncio
async def test_returns_zero_without_calling_eval_for_zero_request() -> None:
    """requested=0 なら eval を叩かず 0 を返す (DoS / 無駄 RT 防止)。"""
    redis = MagicMock()
    redis.eval = AsyncMock()
    granted = await consume_daily_budget(redis, "extract", 0, 600)
    assert granted == 0
    redis.eval.assert_not_called()


@pytest.mark.asyncio
async def test_raises_value_error_for_non_positive_daily_max() -> None:
    """daily_max <= 0 は configuration error として例外。"""
    redis = MagicMock()
    redis.eval = AsyncMock()
    with pytest.raises(ValueError, match="daily_max must be positive"):
        await consume_daily_budget(redis, "extract", 10, 0)


@pytest.mark.asyncio
async def test_eval_arguments_include_role_date_key_and_ttl() -> None:
    """eval に渡る (key, requested, daily_max, ttl) が仕様どおり。"""
    redis = MagicMock()
    redis.eval = AsyncMock(return_value=5)
    await consume_daily_budget(redis, "assess", 5, 600)

    call_args = redis.eval.call_args.args
    # eval(script, numkeys, key, requested, daily_max, ttl)
    assert call_args[1] == 1
    assert call_args[2].startswith("backfill:budget:assess:")
    assert int(call_args[3]) == 5
    assert int(call_args[4]) == 600
    assert int(call_args[5]) == 26 * 60 * 60
