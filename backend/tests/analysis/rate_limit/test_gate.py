"""``ProviderRateLimitGate.acquire`` の振る舞いテスト。

3 stage (extraction/assessment/embedding) 共通の rate limit facade として、

1) ``acquire(policy) -> bool`` の戻り値契約 (RPD/RPM 両 acquire の結果を bool に圧縮)
2) Redis key 名前空間 (provider × model で stage 横断共有) の構造的不変条件

の 2 観点を gate の振る舞い経由で検証する。``_build_limiters`` は module-private
関数として gate.py に内包されており、外部からは直接 import せず gate の振る舞い
経由でのみテストする。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.analysis.rate_limit import ProviderRateLimitGate, RatePolicy
from app.redis.sliding_window import RateLimitExceededError


def _policy() -> RatePolicy:
    return RatePolicy(provider="gemini", model="m", rpm=100, rpd=1500)


class TestAcquireReturnValue:
    """``acquire(policy) -> bool`` の戻り値契約。"""

    @pytest.mark.asyncio
    async def test_returns_true_when_both_limiters_succeed(self) -> None:
        rpm = MagicMock()
        rpm.acquire = AsyncMock(return_value=None)
        rpd = MagicMock()
        rpd.acquire = AsyncMock(return_value=None)
        with patch(
            "app.analysis.rate_limit.gate._build_limiters",
            return_value=(rpm, rpd),
        ):
            gate = ProviderRateLimitGate()
            assert await gate.acquire(_policy()) is True
        rpd.acquire.assert_awaited_once()
        rpm.acquire.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_false_when_rpd_exceeded(self) -> None:
        """rpd 評価で quota 超過なら rpm に到達せず ``False``。"""
        rpm = MagicMock()
        rpm.acquire = AsyncMock(return_value=None)
        rpd = MagicMock()
        rpd.acquire = AsyncMock(side_effect=RateLimitExceededError("rpd"))
        with patch(
            "app.analysis.rate_limit.gate._build_limiters",
            return_value=(rpm, rpd),
        ):
            gate = ProviderRateLimitGate()
            assert await gate.acquire(_policy()) is False
        rpd.acquire.assert_awaited_once()
        rpm.acquire.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returns_false_when_rpm_exceeded(self) -> None:
        rpm = MagicMock()
        rpm.acquire = AsyncMock(side_effect=RateLimitExceededError("rpm"))
        rpd = MagicMock()
        rpd.acquire = AsyncMock(return_value=None)
        with patch(
            "app.analysis.rate_limit.gate._build_limiters",
            return_value=(rpm, rpd),
        ):
            gate = ProviderRateLimitGate()
            assert await gate.acquire(_policy()) is False
        rpd.acquire.assert_awaited_once()
        rpm.acquire.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_true_when_both_limiters_none(self) -> None:
        """RPM/RPD 未設定 policy (Stage 5 embedder 想定) で True を返す。"""
        with patch(
            "app.analysis.rate_limit.gate._build_limiters",
            return_value=(None, None),
        ):
            gate = ProviderRateLimitGate()
            policy = RatePolicy(provider="gemini", model="m", rpm=None, rpd=None)
            assert await gate.acquire(policy) is True

    @pytest.mark.asyncio
    async def test_no_limit_policy_does_not_touch_redis(self) -> None:
        """RPM/RPD 未設定なら Redis 接続を作らず ``True`` を返す。"""
        policy = RatePolicy(provider="gemini", model="m", rpm=None, rpd=None)
        with patch("app.analysis.rate_limit.gate.get_redis") as get_redis:
            gate = ProviderRateLimitGate()
            assert await gate.acquire(policy) is True
        get_redis.assert_not_called()


class TestRedisKeyContract:
    """gate.acquire 経由で Redis に渡される key の構造的不変条件。

    旧 ``test_limiter_factory.py`` の ``_build_limiters`` 単体テストを、private
    関数を露出させずに gate の振る舞い経由で検証する形に書き換えたもの。
    """

    @staticmethod
    def _patched_redis() -> tuple[MagicMock, AsyncMock]:
        """``register_script`` の戻り (= script mock) を [1, 0, "1000.0"] で
        即時成功させる Redis mock を返す。
        """
        redis_mock = MagicMock()
        script = AsyncMock(return_value=[1, 0, "1000.0"])
        redis_mock.register_script.return_value = script
        return redis_mock, script

    @pytest.mark.asyncio
    async def test_same_provider_model_shares_redis_keys(self) -> None:
        """同 provider × 同 model なら、stage が違っても同じ Redis key を叩く。

        Gemini 公式の rate limit は project × model で適用される。
        stage (extract/assess/embed) は key 設計に影響しない。
        """
        redis_mock, script = self._patched_redis()
        policy = RatePolicy(
            provider="gemini", model="gemini-2.5-flash-lite", rpm=100, rpd=1500
        )
        with patch("app.analysis.rate_limit.gate.get_redis", return_value=redis_mock):
            gate = ProviderRateLimitGate()
            await gate.acquire(policy)
            await gate.acquire(policy)
        keys = {call.kwargs["keys"][0] for call in script.call_args_list}
        assert keys == {
            "ratelimit:gemini:gemini-2.5-flash-lite:rpd",
            "ratelimit:gemini:gemini-2.5-flash-lite:rpm",
        }

    @pytest.mark.asyncio
    async def test_different_provider_distinct_redis_keys(self) -> None:
        """同 model 名でも provider が違えば key は分離される。"""
        redis_mock, script = self._patched_redis()
        with patch("app.analysis.rate_limit.gate.get_redis", return_value=redis_mock):
            gate = ProviderRateLimitGate()
            await gate.acquire(
                RatePolicy(provider="gemini", model="m", rpm=100, rpd=1500)
            )
            await gate.acquire(
                RatePolicy(provider="deepseek", model="m", rpm=100, rpd=1500)
            )
        keys = {call.kwargs["keys"][0] for call in script.call_args_list}
        assert "ratelimit:gemini:m:rpd" in keys
        assert "ratelimit:deepseek:m:rpd" in keys
        assert "ratelimit:gemini:m:rpm" in keys
        assert "ratelimit:deepseek:m:rpm" in keys

    @pytest.mark.asyncio
    async def test_different_model_distinct_redis_keys(self) -> None:
        """同 provider でも model が違えば key は分離される。"""
        redis_mock, script = self._patched_redis()
        with patch("app.analysis.rate_limit.gate.get_redis", return_value=redis_mock):
            gate = ProviderRateLimitGate()
            await gate.acquire(
                RatePolicy(provider="gemini", model="flash", rpm=100, rpd=1500)
            )
            await gate.acquire(
                RatePolicy(provider="gemini", model="pro", rpm=100, rpd=1500)
            )
        keys = {call.kwargs["keys"][0] for call in script.call_args_list}
        assert "ratelimit:gemini:flash:rpd" in keys
        assert "ratelimit:gemini:pro:rpd" in keys
        assert "ratelimit:gemini:flash:rpm" in keys
        assert "ratelimit:gemini:pro:rpm" in keys

    @pytest.mark.asyncio
    async def test_rpm_none_skips_rpm_call(self) -> None:
        """``rpm=None`` なら RPM 用の Redis call は発生せず、RPD のみ呼ばれる。"""
        redis_mock, script = self._patched_redis()
        with patch("app.analysis.rate_limit.gate.get_redis", return_value=redis_mock):
            gate = ProviderRateLimitGate()
            await gate.acquire(
                RatePolicy(provider="gemini", model="m", rpm=None, rpd=1500)
            )
        keys = [call.kwargs["keys"][0] for call in script.call_args_list]
        assert keys == ["ratelimit:gemini:m:rpd"]

    @pytest.mark.asyncio
    async def test_rpd_none_skips_rpd_call(self) -> None:
        """``rpd=None`` なら RPD 用の Redis call は発生せず、RPM のみ呼ばれる。"""
        redis_mock, script = self._patched_redis()
        with patch("app.analysis.rate_limit.gate.get_redis", return_value=redis_mock):
            gate = ProviderRateLimitGate()
            await gate.acquire(
                RatePolicy(provider="gemini", model="m", rpm=100, rpd=None)
            )
        keys = [call.kwargs["keys"][0] for call in script.call_args_list]
        assert keys == ["ratelimit:gemini:m:rpm"]
