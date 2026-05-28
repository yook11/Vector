"""curation provider hold (一時停止フラグ) のユニットテスト。

検証する不変条件:
- ``set_curation_hold`` は TTL 付きで key を書く
- ``is_curation_held`` は key の有無を素直に反映する
- Redis 障害時は **fail-open** (check は False、set は例外を握って業務を壊さない)
- TTL は spec 値 (6h)
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.queue.helpers.stage_hold import (
    is_curation_held,
    set_curation_hold,
)

_CURATION_HOLD_KEY = "curation:hold"
_HOLD_TTL_SECONDS = 6 * 60 * 60


def test_hold_ttl_matches_spec() -> None:
    """hold TTL は spec で決めた 6h (= 21600 秒)。"""
    assert _HOLD_TTL_SECONDS == 6 * 60 * 60


@pytest.mark.asyncio
async def test_set_curation_hold_writes_key_with_ttl() -> None:
    """set は key に reason を TTL 付きで書く。"""
    redis = AsyncMock()
    await set_curation_hold(redis, reason="ai_error_configuration")
    redis.set.assert_awaited_once_with(
        _CURATION_HOLD_KEY, "ai_error_configuration", ex=_HOLD_TTL_SECONDS
    )


@pytest.mark.asyncio
async def test_is_curation_held_true_when_key_exists() -> None:
    """key があれば held=True。"""
    redis = AsyncMock()
    redis.exists = AsyncMock(return_value=1)
    assert await is_curation_held(redis) is True
    redis.exists.assert_awaited_once_with(_CURATION_HOLD_KEY)


@pytest.mark.asyncio
async def test_is_curation_held_false_when_absent() -> None:
    """key が無ければ held=False。"""
    redis = AsyncMock()
    redis.exists = AsyncMock(return_value=0)
    assert await is_curation_held(redis) is False


@pytest.mark.asyncio
async def test_is_curation_held_fails_open_on_redis_error() -> None:
    """Redis 障害時は False (= 救済を止めない fail-open)。"""
    redis = AsyncMock()
    redis.exists = AsyncMock(side_effect=ConnectionError("redis down"))
    assert await is_curation_held(redis) is False


@pytest.mark.asyncio
async def test_set_curation_hold_swallows_redis_error() -> None:
    """set の Redis 障害は伝播しない (失敗ハンドラを落とさない)。"""
    redis = AsyncMock()
    redis.set = AsyncMock(side_effect=ConnectionError("redis down"))
    # 例外が出ないこと自体が不変条件 (assert は到達 = 非伝播の証明)
    await set_curation_hold(redis, reason="ai_error_configuration")
