"""assessment / embedding hold helper の Redis 契約テスト。"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any, cast
from unittest.mock import AsyncMock, call

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.queue.helpers.stage_hold import (
    is_assessment_held,
    is_embedding_held,
    set_assessment_hold,
    set_embedding_hold,
)

SetHold = Callable[..., Awaitable[None]]
IsHeld = Callable[[Any], Awaitable[bool]]
RecoveryHoldOperation = Callable[..., Awaitable[bool]]


def _recovery_hold_operation(name: str) -> RecoveryHoldOperation:
    """未実装APIをcollection errorではなく契約failureとして報告する。"""
    import app.queue.helpers.stage_hold as stage_hold

    operation = getattr(stage_hold, name, None)
    if operation is None:
        pytest.fail(f"stage_hold.{name} is not implemented")
    return cast("RecoveryHoldOperation", operation)


def _owned_lua_contract(
    fake_redis: AsyncMock,
    *,
    command: str,
) -> tuple[int, tuple[object, ...], bool, bool, bool, int, int, int]:
    """eval引数とcompare-and-commandの最小Lua契約を観測可能値へ変換する。"""
    script, numkeys, *args = fake_redis.eval.await_args.args
    canonical = re.sub(r"[\s\"']+", "", script).upper()
    return (
        numkeys,
        tuple(args),
        "REDIS.CALL(GET,KEYS[1])" in canonical,
        "==ARGV[1]" in canonical,
        f"REDIS.CALL({command},KEYS[1]" in canonical,
        fake_redis.get.await_count,
        fake_redis.expire.await_count,
        fake_redis.delete.await_count,
    )


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "key"),
    [("curation", "curation:hold"), ("assessment", "assessment:hold")],
)
@pytest.mark.parametrize(
    ("set_result", "expected"),
    [(True, True), (None, False)],
    ids=["acquired", "existing-hold"],
)
async def test_acquire_recovery_hold_uses_owned_token_without_overwrite(
    stage: str,
    key: str,
    set_result: bool | None,
    expected: bool,
) -> None:
    """recovery holdはstage keyへ一意tokenをNX・6h TTL付きで取得する。"""
    fake_redis = AsyncMock()
    fake_redis.set.return_value = set_result
    acquire = _recovery_hold_operation("acquire_recovery_hold")

    acquired = await acquire(fake_redis, stage=stage, token="recovery-token")

    assert (acquired, fake_redis.set.await_args) == (
        expected,
        call(key, "recovery-token", nx=True, ex=21_600),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "key"),
    [("curation", "curation:hold"), ("assessment", "assessment:hold")],
)
@pytest.mark.parametrize(
    ("eval_result", "expected"),
    [(1, True), (0, False)],
    ids=["owned", "not-owned-or-missing"],
)
async def test_refresh_recovery_hold_is_atomic_and_token_owned(
    stage: str,
    key: str,
    eval_result: int,
    expected: bool,
) -> None:
    """refreshはGET一致時だけEXPIREするLuaを使い、直接操作を行わない。"""
    fake_redis = AsyncMock()
    fake_redis.eval.return_value = eval_result
    refresh = _recovery_hold_operation("refresh_recovery_hold")

    refreshed = await refresh(fake_redis, stage=stage, token="recovery-token")

    assert (refreshed, _owned_lua_contract(fake_redis, command="EXPIRE")) == (
        expected,
        (
            1,
            (key, "recovery-token", 21_600),
            True,
            True,
            True,
            0,
            0,
            0,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "key"),
    [("curation", "curation:hold"), ("assessment", "assessment:hold")],
)
@pytest.mark.parametrize(
    ("eval_result", "expected"),
    [(1, True), (0, False)],
    ids=["owned", "not-owned-or-missing"],
)
async def test_release_recovery_hold_is_atomic_and_token_owned(
    stage: str,
    key: str,
    eval_result: int,
    expected: bool,
) -> None:
    """releaseはGET一致時だけDELするLuaを使い、provider holdを直接消さない。"""
    fake_redis = AsyncMock()
    fake_redis.eval.return_value = eval_result
    release = _recovery_hold_operation("release_recovery_hold")

    released = await release(fake_redis, stage=stage, token="recovery-token")

    assert (released, _owned_lua_contract(fake_redis, command="DEL")) == (
        expected,
        (
            1,
            (key, "recovery-token"),
            True,
            True,
            True,
            0,
            0,
            0,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation_name",
    ["acquire_recovery_hold", "refresh_recovery_hold", "release_recovery_hold"],
)
async def test_recovery_hold_rejects_embedding_stage(operation_name: str) -> None:
    """recovery ownership helperはanalysisのcuration / assessmentだけを対象にする。"""
    fake_redis = AsyncMock()
    operation = _recovery_hold_operation(operation_name)

    with pytest.raises(ValueError, match="embedding"):
        await operation(fake_redis, stage="embedding", token="recovery-token")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation_name",
    ["acquire_recovery_hold", "refresh_recovery_hold", "release_recovery_hold"],
)
async def test_recovery_hold_does_not_report_redis_failure_as_success(
    operation_name: str,
) -> None:
    """recovery操作のRedis障害は成功boolへ変換せずcallerへ伝播する。"""
    fake_redis = AsyncMock()
    fake_redis.set.side_effect = RedisConnectionError("connection refused")
    fake_redis.eval.side_effect = RedisConnectionError("connection refused")
    operation = _recovery_hold_operation(operation_name)

    with pytest.raises(RedisConnectionError, match="connection refused"):
        await operation(fake_redis, stage="curation", token="recovery-token")
