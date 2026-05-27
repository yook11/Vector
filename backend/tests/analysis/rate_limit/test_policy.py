"""``RatePolicy`` VO のテスト。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.analysis.rate_limit import RatePolicy


def test_rate_policy_keeps_provider_model_quota_values() -> None:
    """provider/model/rpm/rpd をそのまま保持する。"""
    policy = RatePolicy(provider="gemini", model="flash", rpm=100, rpd=1500)

    assert policy.provider == "gemini"
    assert policy.model == "flash"
    assert policy.rpm == 100
    assert policy.rpd == 1500


class TestRatePolicyValidation:
    """``__post_init__`` validation が不正な設定値を弾く。"""

    @pytest.mark.parametrize(
        "kwargs",
        [
            # 空 str
            {"provider": "", "model": "m", "rpm": 100, "rpd": 1500},
            # 非 str provider
            {"provider": MagicMock(), "model": "m", "rpm": 100, "rpd": 1500},
            # 非正 int rpm
            {"provider": "p", "model": "m", "rpm": 0, "rpd": 1500},
            # 非 int rpd
            {"provider": "p", "model": "m", "rpm": 100, "rpd": "1500"},
        ],
    )
    def test_rate_policy_rejects_invalid_inputs(
        self, kwargs: dict[str, object]
    ) -> None:
        """空 str / 非 str / 非正 int を ``__post_init__`` で拒否する。"""
        with pytest.raises(ValueError):
            RatePolicy(**kwargs)  # type: ignore[arg-type]
