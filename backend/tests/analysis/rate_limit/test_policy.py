"""``RatePolicy`` VO のテスト。

duck-typed Protocol (``RatePolicySource``) で AI component の ClassVar から
policy を組み立てる契約と、``__post_init__`` validation で MagicMock の silent
漏れ等を弾く構造的 guard を検証する。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.analysis.rate_limit import RatePolicy


class TestRatePolicyFromComponent:
    """``RatePolicy.from_component`` の ClassVar 読込が期待通り動くこと。"""

    def test_from_component_reads_classvars(self) -> None:
        """``PROVIDER`` / ``MODEL`` / ``RPM`` / ``RPD`` を持つ stub class から
        正しく値を読む。
        """

        class StubClass:
            PROVIDER = "gemini"
            MODEL = "flash"
            RPM = 100
            RPD = 1500

        policy = RatePolicy.from_component(StubClass())

        assert policy.provider == "gemini"
        assert policy.model == "flash"
        assert policy.rpm == 100
        assert policy.rpd == 1500
        assert policy.rpm_key == "ratelimit:gemini:flash:rpm"
        assert policy.rpd_key == "ratelimit:gemini:flash:rpd"


class TestRatePolicyValidation:
    """``__post_init__`` validation が duck typing の弱点 (MagicMock 含む) を弾く。"""

    @pytest.mark.parametrize(
        "kwargs",
        [
            # 空 str
            {"provider": "", "model": "m", "rpm": 100, "rpd": 1500},
            # 非 str provider (MagicMock の未定義属性は MagicMock を返す)
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
        """空 str / 非 str / 非正 int / MagicMock を ``__post_init__`` で拒否する。"""
        with pytest.raises(ValueError):
            RatePolicy(**kwargs)  # type: ignore[arg-type]

    def test_from_component_rejects_silent_magicmock_provider(self) -> None:
        """``PROVIDER`` 未定義の MagicMock を ``from_component`` 経由で渡しても
        ``__post_init__`` が ``ValueError`` を起こす。

        ``MagicMock()`` は未定義属性アクセスで MagicMock を返すので getattr の
        default に到達しない。ratelimit キーが silent に
        ``ratelimit:<MagicMock...>:model:rpm`` 化することを構造的に防ぐ。
        """
        mock_component = MagicMock()
        mock_component.MODEL = "m"
        mock_component.RPM = 100
        mock_component.RPD = 1500
        # 意図的に PROVIDER を設定しない (MagicMock が黙って MagicMock を返す)

        with pytest.raises(ValueError, match="provider must be non-empty str"):
            RatePolicy.from_component(mock_component)


def test_simplenamespace_also_works_as_source() -> None:
    """SimpleNamespace で属性を生やしたものも duck-typed で受け入れる。"""
    component = SimpleNamespace(PROVIDER="gemini", MODEL="m", RPM=100, RPD=1500)
    policy = RatePolicy.from_component(component)
    assert policy.rpm_key == "ratelimit:gemini:m:rpm"
