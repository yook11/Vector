"""assessment / embedding hold helper の Redis 契約テスト。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.analysis.assessment.hold import is_assessment_held, set_assessment_hold
from app.analysis.embedding.hold import is_embedding_held, set_embedding_hold

SetHold = Callable[..., Awaitable[None]]
IsHeld = Callable[[Any], Awaitable[bool]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("set_hold", "key"),
    [
        (set_assessment_hold, "assessment:hold"),
        (set_embedding_hold, "embedding:hold"),
    ],
)
async def test_set_hold_writes_stage_key_with_six_hour_ttl(
    set_hold: SetHold,
    key: str,
) -> None:
    """hold set は stage 固有 key に 6h TTL 付きで reason を保存する。"""
    fake_redis = AsyncMock()

    await set_hold(fake_redis, reason="ai_error_configuration")

    fake_redis.set.assert_awaited_once_with(
        key,
        "ai_error_configuration",
        ex=6 * 60 * 60,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("is_held", "key"),
    [
        (is_assessment_held, "assessment:hold"),
        (is_embedding_held, "embedding:hold"),
    ],
)
async def test_is_hold_reads_stage_key(
    is_held: IsHeld,
    key: str,
) -> None:
    """exists の truthy / falsy を bool に正規化して返す。"""
    fake_redis = AsyncMock()
    fake_redis.exists.return_value = 1

    assert await is_held(fake_redis) is True
    fake_redis.exists.assert_awaited_once_with(key)

    fake_redis.exists.reset_mock()
    fake_redis.exists.return_value = 0
    assert await is_held(fake_redis) is False
    fake_redis.exists.assert_awaited_once_with(key)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "is_held",
    [is_assessment_held, is_embedding_held],
)
async def test_is_hold_fail_open_on_redis_error(is_held: IsHeld) -> None:
    """Redis 障害時は cron 救済を止めないため fail-open する。"""
    fake_redis = AsyncMock()
    fake_redis.exists.side_effect = RedisConnectionError("connection refused")

    assert await is_held(fake_redis) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "set_hold",
    [set_assessment_hold, set_embedding_hold],
)
async def test_set_hold_swallows_redis_error(set_hold: SetHold) -> None:
    """hold set は best-effort なので Redis 障害で caller を落とさない。"""
    fake_redis = AsyncMock()
    fake_redis.set.side_effect = RedisConnectionError("connection refused")

    await set_hold(fake_redis, reason="ai_error_configuration")
