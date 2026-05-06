"""consume_search_quota のユニットテスト (Lua eval を MagicMock)。

`tests/test_maintenance_budget.py` の MagicMock + AsyncMock パターンを完全踏襲。
red-team C1 対策の構造的不変条件を assertion で固定する。
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.search.quota import SearchQuotaExceededError, consume_search_quota


@pytest.mark.asyncio
async def test_returns_granted_when_below_max() -> None:
    """Lua スクリプトが許可数を返したら関数も同じ値を返す。"""
    redis = MagicMock()
    redis.eval = AsyncMock(return_value=1)
    granted = await consume_search_quota(redis, uuid.uuid4(), 1, 100)
    assert granted == 1


@pytest.mark.asyncio
async def test_raises_quota_exceeded_when_lua_returns_zero() -> None:
    """既に当日 daily_max に達していれば SearchQuotaExceededError を送出する。

    HTTP 層に伝えやすいよう budget.py と異なり 0 を例外に昇格する設計。
    """
    redis = MagicMock()
    redis.eval = AsyncMock(return_value=0)
    with pytest.raises(SearchQuotaExceededError):
        await consume_search_quota(redis, uuid.uuid4(), 1, 100)


@pytest.mark.asyncio
async def test_returns_zero_without_calling_eval_for_zero_request() -> None:
    """requested=0 なら eval を叩かず 0 を返す (caller の defensive call 用)。"""
    redis = MagicMock()
    redis.eval = AsyncMock()
    granted = await consume_search_quota(redis, uuid.uuid4(), 0, 100)
    assert granted == 0
    redis.eval.assert_not_called()


@pytest.mark.asyncio
async def test_raises_value_error_for_non_positive_daily_max() -> None:
    """daily_max <= 0 は configuration error として例外。"""
    redis = MagicMock()
    redis.eval = AsyncMock()
    with pytest.raises(ValueError, match="daily_max must be positive"):
        await consume_search_quota(redis, uuid.uuid4(), 1, 0)


@pytest.mark.asyncio
async def test_eval_arguments_include_user_id_date_key_and_ttl() -> None:
    """eval に渡る (key, requested, daily_max, ttl) が仕様どおり。

    - key prefix: ``search:quota:user:`` (rate-limit ZSET と namespace 分離)
    - daily_max: そのまま渡る
    - ttl: 26h (日跨ぎ猶予、budget.py と同ポリシ)
    """
    redis = MagicMock()
    redis.eval = AsyncMock(return_value=1)
    user_id = uuid.uuid4()
    await consume_search_quota(redis, user_id, 1, 100)

    call_args = redis.eval.call_args.args
    # eval(script, numkeys, key, requested, daily_max, ttl)
    assert call_args[1] == 1
    assert call_args[2].startswith(f"search:quota:user:{user_id}:")
    assert int(call_args[3]) == 1
    assert int(call_args[4]) == 100
    assert int(call_args[5]) == 26 * 60 * 60


@pytest.mark.asyncio
async def test_quota_keys_are_per_user_independent() -> None:
    """別 user の counter は別 key (user A の枯渇が user B に波及しない)。"""
    redis = MagicMock()
    redis.eval = AsyncMock(return_value=1)
    u1 = uuid.uuid4()
    u2 = uuid.uuid4()
    await consume_search_quota(redis, u1, 1, 100)
    await consume_search_quota(redis, u2, 1, 100)

    keys = [c.args[2] for c in redis.eval.call_args_list]
    assert keys[0] != keys[1]
    assert all(k.startswith("search:quota:user:") for k in keys)
