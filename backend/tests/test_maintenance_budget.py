"""Python 側の入力ガードと引数配線の unit テスト。

eval 戻り値の int 変換 / requested<=0 の短絡 / daily_max<=0 の ValueError /
eval 引数形と key 組立 (日付・role 成分) を確認する。
Lua 本体の振る舞いの正本は tests/queue/test_budget_integration.py。
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.queue.helpers.budget import consume_daily_budget


@pytest.mark.asyncio
@pytest.mark.parametrize("eval_result", [10, 0])
async def test_returns_lua_eval_result_as_int(eval_result: int) -> None:
    """関数は Lua eval の戻り値をそのまま int として返す (パススルー契約)。"""
    redis = MagicMock()
    redis.eval = AsyncMock(return_value=eval_result)
    granted = await consume_daily_budget(redis, "extract", 10, 600)
    assert granted == eval_result


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


@pytest.mark.asyncio
async def test_key_date_component_changes_across_day_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """日付 D→D+1 で消費すると key の YYYYMMDD 成分が変わる。

    日付成分が固定文字列に退行すると予算が日次リセットされず永久に回復しない。
    """
    redis = MagicMock()
    redis.eval = AsyncMock(return_value=5)

    monkeypatch.setattr(
        "app.queue.helpers.budget.utc_now",
        lambda: datetime(2026, 8, 13, 12, 0, tzinfo=UTC),
    )
    await consume_daily_budget(redis, "extract", 5, 600)
    key_day_one = redis.eval.call_args.args[2]

    monkeypatch.setattr(
        "app.queue.helpers.budget.utc_now",
        lambda: datetime(2026, 8, 14, 12, 0, tzinfo=UTC),
    )
    await consume_daily_budget(redis, "extract", 5, 600)
    key_day_two = redis.eval.call_args.args[2]

    assert key_day_one != key_day_two
    assert key_day_one.endswith("20260813")
    assert key_day_two.endswith("20260814")


@pytest.mark.asyncio
async def test_key_role_component_differs_by_role() -> None:
    """role が異なれば eval に渡る key も異なる (role ごとに独立した予算)。"""
    redis = MagicMock()
    redis.eval = AsyncMock(return_value=5)

    await consume_daily_budget(redis, "curate", 5, 600)
    key_curate = redis.eval.call_args.args[2]

    await consume_daily_budget(redis, "assess", 5, 600)
    key_assess = redis.eval.call_args.args[2]

    assert key_curate != key_assess
