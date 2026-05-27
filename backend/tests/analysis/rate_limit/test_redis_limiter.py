"""Redis ZSET スライディングウィンドウ方式のレートリミッターのテスト。"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.redis.sliding_window import RateLimitExceededError, SlidingWindowLimiter


def _make_mock_redis(script_results: list[list]) -> MagicMock:
    """register_script をスクリプト化したモック Redis クライアントを作成する。"""
    mock_redis = MagicMock()
    mock_script = AsyncMock(side_effect=script_results)
    mock_redis.register_script.return_value = mock_script
    return mock_redis


@pytest.mark.asyncio
async def test_acquire_succeeds_when_under_limit() -> None:
    """空きあり: スクリプトが [1, 0, now] を返し acquire が成功する。"""
    mock_redis = _make_mock_redis([[1, 0, "1000.0"]])
    limiter = SlidingWindowLimiter(
        redis=mock_redis, key="test:rpm", max_requests=10, window_seconds=60
    )
    await limiter.acquire()  # 例外を送出しないこと


@pytest.mark.asyncio
async def test_acquire_raises_when_non_blocking_and_full() -> None:
    """非ブロックモードでは上限到達時に RateLimitExceededError を送出する。"""
    mock_redis = _make_mock_redis([[0, "990.0", "1000.0"]])
    limiter = SlidingWindowLimiter(
        redis=mock_redis,
        key="test:rpd",
        max_requests=100,
        window_seconds=86400,
        block=False,
    )
    with pytest.raises(RateLimitExceededError, match="Rate limit exceeded"):
        await limiter.acquire()


@pytest.mark.asyncio
async def test_acquire_blocking_waits_then_succeeds() -> None:
    """ブロックモード: 初回は満杯、sleep 後の 2 回目で成功する。"""
    mock_redis = _make_mock_redis(
        [
            [0, "999.0", "1000.0"],  # 1 回目: 満杯
            [1, 0, "1060.1"],  # 2 回目: 空きができた
        ]
    )
    limiter = SlidingWindowLimiter(
        redis=mock_redis, key="test:rpm", max_requests=10, window_seconds=60
    )
    with patch("app.redis.sliding_window.asyncio.sleep", AsyncMock()) as mock_sleep:
        await limiter.acquire()

    # sleep は 1 回: (999.0 + 60) - 1000.0 = 59.0 であるはず
    mock_sleep.assert_called_once_with(59.0)


@pytest.mark.asyncio
async def test_lua_script_receives_correct_args() -> None:
    """Lua スクリプトが正しい keys と args で呼び出されることを検証する。"""
    mock_redis = _make_mock_redis([[1, 0, "1000.0"]])
    limiter = SlidingWindowLimiter(
        redis=mock_redis, key="ratelimit:model:rpm", max_requests=500, window_seconds=60
    )

    with patch("app.redis.sliding_window.uuid.uuid4") as mock_uuid:
        mock_uuid.return_value.hex = "abc123"
        await limiter.acquire()

    script = mock_redis.register_script.return_value
    script.assert_called_once()
    call_kwargs = script.call_args
    assert call_kwargs.kwargs["keys"] == ["ratelimit:model:rpm"]
    args = call_kwargs.kwargs["args"]
    assert args[0] == 500  # max_requests であること
    assert args[1] == 60  # window_seconds であること
    assert args[2] == "abc123"  # member であること
    assert args[3] == 120  # ttl = 60 + 60 であること
