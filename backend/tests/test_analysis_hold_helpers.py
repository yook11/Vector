"""assessment / embedding hold helper の Redis 契約テスト。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.audit.domain.event import Stage
from app.queue.helpers.stage_hold import HoldableStage, is_stage_held, set_stage_hold


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "key"),
    [
        (Stage.ASSESSMENT, "assessment:hold"),
        (Stage.EMBEDDING, "embedding:hold"),
    ],
)
async def test_set_hold_writes_stage_key_with_six_hour_ttl(
    stage: HoldableStage,
    key: str,
) -> None:
    """hold set は stage 固有 key に 6h TTL 付きで reason を保存する。"""
    fake_redis = AsyncMock()

    await set_stage_hold(fake_redis, stage, reason="ai_error_configuration")

    fake_redis.set.assert_awaited_once_with(
        key,
        "ai_error_configuration",
        ex=6 * 60 * 60,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "key"),
    [
        (Stage.ASSESSMENT, "assessment:hold"),
        (Stage.EMBEDDING, "embedding:hold"),
    ],
)
async def test_is_hold_reads_stage_key(
    stage: HoldableStage,
    key: str,
) -> None:
    """exists の truthy / falsy を bool に正規化して返す。"""
    fake_redis = AsyncMock()
    fake_redis.exists.return_value = 1

    assert await is_stage_held(fake_redis, stage) is True
    fake_redis.exists.assert_awaited_once_with(key)

    fake_redis.exists.reset_mock()
    fake_redis.exists.return_value = 0
    assert await is_stage_held(fake_redis, stage) is False
    fake_redis.exists.assert_awaited_once_with(key)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [Stage.ASSESSMENT, Stage.EMBEDDING])
async def test_is_hold_fail_open_on_redis_error(stage: HoldableStage) -> None:
    """Redis 障害時は cron 救済を止めないため fail-open する。"""
    fake_redis = AsyncMock()
    fake_redis.exists.side_effect = RedisConnectionError("connection refused")

    assert await is_stage_held(fake_redis, stage) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [Stage.ASSESSMENT, Stage.EMBEDDING])
async def test_set_hold_swallows_redis_error(stage: HoldableStage) -> None:
    """hold set は best-effort なので Redis 障害で caller を落とさない。"""
    fake_redis = AsyncMock()
    fake_redis.set.side_effect = RedisConnectionError("connection refused")

    await set_stage_hold(fake_redis, stage, reason="ai_error_configuration")
