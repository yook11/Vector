"""Tests for the Redis ZSET sliding-window rate limiter."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.infra.redis.rate_limiter import RateLimiter, RateLimitExceededError


def _make_mock_redis(script_results: list[list]) -> MagicMock:
    """Create a mock Redis client with a scripted register_script."""
    mock_redis = MagicMock()
    mock_script = AsyncMock(side_effect=script_results)
    mock_redis.register_script.return_value = mock_script
    return mock_redis


@pytest.mark.asyncio
async def test_acquire_succeeds_when_under_limit() -> None:
    """Slot available: script returns [1, 0, now] and acquire returns."""
    mock_redis = _make_mock_redis([[1, 0, "1000.0"]])
    limiter = RateLimiter(
        redis=mock_redis, key="test:rpm", max_requests=10, window_seconds=60
    )
    await limiter.acquire()  # should not raise


@pytest.mark.asyncio
async def test_acquire_raises_when_non_blocking_and_full() -> None:
    """Non-blocking mode raises RateLimitExceededError at capacity."""
    mock_redis = _make_mock_redis([[0, "990.0", "1000.0"]])
    limiter = RateLimiter(
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
    """Blocking mode: first call full, second call succeeds after sleep."""
    mock_redis = _make_mock_redis(
        [
            [0, "999.0", "1000.0"],  # first: at capacity
            [1, 0, "1060.1"],  # second: slot freed
        ]
    )
    limiter = RateLimiter(
        redis=mock_redis, key="test:rpm", max_requests=10, window_seconds=60
    )
    with patch("app.infra.redis.rate_limiter.asyncio.sleep", AsyncMock()) as mock_sleep:
        await limiter.acquire()

    # Should have slept once: (999.0 + 60) - 1000.0 = 59.0
    mock_sleep.assert_called_once_with(59.0)


@pytest.mark.asyncio
async def test_lua_script_receives_correct_args() -> None:
    """Verify the Lua script is called with the right keys and args."""
    mock_redis = _make_mock_redis([[1, 0, "1000.0"]])
    limiter = RateLimiter(
        redis=mock_redis, key="ratelimit:model:rpm", max_requests=500, window_seconds=60
    )

    with patch("app.infra.redis.rate_limiter.uuid.uuid4") as mock_uuid:
        mock_uuid.return_value.hex = "abc123"
        await limiter.acquire()

    script = mock_redis.register_script.return_value
    script.assert_called_once()
    call_kwargs = script.call_args
    assert call_kwargs.kwargs["keys"] == ["ratelimit:model:rpm"]
    args = call_kwargs.kwargs["args"]
    assert args[0] == 500  # max_requests
    assert args[1] == 60  # window_seconds
    assert args[2] == "abc123"  # member
    assert args[3] == 120  # ttl = 60 + 60
