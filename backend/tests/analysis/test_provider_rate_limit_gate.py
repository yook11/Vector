"""``ProviderRateLimitGate.acquire`` の振る舞いテスト。

``_build_limiters`` を mock し、2 段 acquire (RPD → RPM) の成功/失敗が
bool として正しく現れることを検証する。Stage 4/5 を後続 PR で寄せる前提の
``acquire(policy) -> bool`` interface を構造的に固定する。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.analysis.provider_rate_limit_gate import ProviderRateLimitGate
from app.analysis.rate_limiter import RateLimitExceededError
from app.analysis.rate_policy import RatePolicy


def _policy() -> RatePolicy:
    return RatePolicy(provider="gemini", model="m", rpm=100, rpd=1500)


@pytest.mark.asyncio
async def test_acquire_returns_true_when_both_limiters_succeed() -> None:
    rpm = MagicMock()
    rpm.acquire = AsyncMock(return_value=None)
    rpd = MagicMock()
    rpd.acquire = AsyncMock(return_value=None)
    with patch(
        "app.analysis.provider_rate_limit_gate._build_limiters",
        return_value=(rpm, rpd),
    ):
        gate = ProviderRateLimitGate()
        assert await gate.acquire(_policy()) is True
    rpd.acquire.assert_awaited_once()
    rpm.acquire.assert_awaited_once()


@pytest.mark.asyncio
async def test_acquire_returns_false_when_rpd_exceeded() -> None:
    """rpd 評価で quota 超過なら rpm に到達せず ``False``。"""
    rpm = MagicMock()
    rpm.acquire = AsyncMock(return_value=None)
    rpd = MagicMock()
    rpd.acquire = AsyncMock(side_effect=RateLimitExceededError("rpd"))
    with patch(
        "app.analysis.provider_rate_limit_gate._build_limiters",
        return_value=(rpm, rpd),
    ):
        gate = ProviderRateLimitGate()
        assert await gate.acquire(_policy()) is False
    rpd.acquire.assert_awaited_once()
    rpm.acquire.assert_not_awaited()


@pytest.mark.asyncio
async def test_acquire_returns_false_when_rpm_exceeded() -> None:
    rpm = MagicMock()
    rpm.acquire = AsyncMock(side_effect=RateLimitExceededError("rpm"))
    rpd = MagicMock()
    rpd.acquire = AsyncMock(return_value=None)
    with patch(
        "app.analysis.provider_rate_limit_gate._build_limiters",
        return_value=(rpm, rpd),
    ):
        gate = ProviderRateLimitGate()
        assert await gate.acquire(_policy()) is False
    rpd.acquire.assert_awaited_once()
    rpm.acquire.assert_awaited_once()


@pytest.mark.asyncio
async def test_acquire_returns_true_when_both_limiters_none() -> None:
    """RPM/RPD 未設定 policy (Stage 5 embedder 想定) で True を返す。"""
    with patch(
        "app.analysis.provider_rate_limit_gate._build_limiters",
        return_value=(None, None),
    ):
        gate = ProviderRateLimitGate()
        policy = RatePolicy(provider="gemini", model="m", rpm=None, rpd=None)
        assert await gate.acquire(policy) is True
