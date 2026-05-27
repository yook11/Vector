"""``AIModelRateLimitPolicy`` / ``RateLimitRule`` のテスト。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.analysis.rate_limit import AIModelRateLimitPolicy, RateLimitRule


def test_policy_keeps_provider_model_and_rules() -> None:
    """provider/model と rule 群をそのまま保持する。"""
    rpd = RateLimitRule(
        name="rpd", max_requests=1500, window_seconds=86400, block=False
    )
    rpm = RateLimitRule(name="rpm", max_requests=100, window_seconds=60, block=True)
    policy = AIModelRateLimitPolicy(provider="gemini", model="flash", rules=(rpd, rpm))

    assert policy.provider == "gemini"
    assert policy.model == "flash"
    assert policy.rules == (rpd, rpm)


def test_policy_allows_empty_rules_for_no_limit_model() -> None:
    """制限しないモデルは ``rules=()`` で表す。"""
    policy = AIModelRateLimitPolicy(provider="deepseek", model="flash", rules=())

    assert policy.rules == ()


class TestAIModelRateLimitPolicyValidation:
    """``AIModelRateLimitPolicy`` validation が不正な設定値を弾く。"""

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"provider": "", "model": "m", "rules": ()},
            {"provider": MagicMock(), "model": "m", "rules": ()},
            {"provider": "p", "model": "", "rules": ()},
            {"provider": "p", "model": "m", "rules": []},
            {"provider": "p", "model": "m", "rules": ("not-rule",)},
        ],
    )
    def test_policy_rejects_invalid_inputs(self, kwargs: dict[str, object]) -> None:
        """空 str / 非 str / 非 tuple / 非 rule を拒否する。"""
        with pytest.raises(ValueError):
            AIModelRateLimitPolicy(**kwargs)  # type: ignore[arg-type]


class TestRateLimitRuleValidation:
    """``RateLimitRule`` validation が不正な rule を弾く。"""

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"name": "", "max_requests": 100, "window_seconds": 60, "block": True},
            {"name": "rpm", "max_requests": 0, "window_seconds": 60, "block": True},
            {"name": "rpm", "max_requests": True, "window_seconds": 60, "block": True},
            {"name": "rpm", "max_requests": 100, "window_seconds": 0, "block": True},
            {
                "name": "rpm",
                "max_requests": 100,
                "window_seconds": False,
                "block": True,
            },
            {"name": "rpm", "max_requests": 100, "window_seconds": 60, "block": 1},
        ],
    )
    def test_rule_rejects_invalid_inputs(self, kwargs: dict[str, object]) -> None:
        """空 bucket / 非正 int / bool 混入を拒否する。"""
        with pytest.raises(ValueError):
            RateLimitRule(**kwargs)  # type: ignore[arg-type]
