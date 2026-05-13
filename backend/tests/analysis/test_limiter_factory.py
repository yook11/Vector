"""``_build_limiters`` + ``RatePolicy`` の構造的保証テスト。

PR1 で rate limit のキー名前空間を ``ratelimit:{provider}:{model}:{rpm|rpd}``
に統一した。同一 provider × 同一 model なら呼び出し側 component (extract /
assess / embed のどれでも) で 1 つのカウンタを共有することを構造的に確認する
(provider 側の実 quota と整合)。

Redis 接続は ``app.analysis._limiter_factory.get_redis`` を patch する
(``app.redis.get_redis`` 直接 patch だと module cache を経由しないため fragile)。
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.analysis.rate_policy import RatePolicy


class _StubComponent:
    """``RatePolicySource`` Protocol を構造的に満たす duck-typed stub。"""

    def __init__(
        self,
        provider: str,
        model: str,
        rpm: int | None,
        rpd: int | None,
    ) -> None:
        # dataclass にせず instance 属性で保持することで、AI class が
        # ClassVar で公開している運用と etymology を揃える (``from_component``
        # は ``getattr`` 経由なので instance / class 属性のどちらでも拾える)。
        self.PROVIDER = provider
        self.MODEL = model
        self.RPM = rpm
        self.RPD = rpd


class TestBuildLimitersKeyIsolation:
    """provider × model 粒度の Redis キー独立性 (構造的不変条件)。"""

    def test_same_provider_model_shares_key_across_components(self) -> None:
        """同 provider × 同 model なら component (stage) 違いでもキーが共有される。

        Gemini 公式の rate limit は project × model で適用される。
        ``RatePolicy`` 自体に stage 概念は無いことを表現する。
        """
        from app.analysis._limiter_factory import _build_limiters

        with patch("app.analysis._limiter_factory.get_redis", return_value=MagicMock()):
            policy_a = RatePolicy.from_component(
                _StubComponent("gemini", "gemini-2.5-flash-lite", 100, 1500)
            )
            policy_b = RatePolicy.from_component(
                _StubComponent("gemini", "gemini-2.5-flash-lite", 100, 1500)
            )
            rpm_a, rpd_a = _build_limiters(policy_a)
            rpm_b, rpd_b = _build_limiters(policy_b)

        assert rpm_a is not None and rpm_b is not None
        assert rpd_a is not None and rpd_b is not None
        assert rpm_a._key == rpm_b._key
        assert rpd_a._key == rpd_b._key

    def test_different_provider_distinct_keys(self) -> None:
        """同 model 名でも provider が違えばキーは分かれる。"""
        from app.analysis._limiter_factory import _build_limiters

        with patch("app.analysis._limiter_factory.get_redis", return_value=MagicMock()):
            gemini = _build_limiters(
                RatePolicy.from_component(_StubComponent("gemini", "m", 100, 1500))
            )
            deepseek = _build_limiters(
                RatePolicy.from_component(_StubComponent("deepseek", "m", 100, 1500))
            )

        assert gemini[0] is not None and deepseek[0] is not None
        assert gemini[1] is not None and deepseek[1] is not None
        assert gemini[0]._key != deepseek[0]._key
        assert gemini[1]._key != deepseek[1]._key

    def test_different_model_distinct_keys(self) -> None:
        """同 provider でも model が違えばキーは分かれる。"""
        from app.analysis._limiter_factory import _build_limiters

        with patch("app.analysis._limiter_factory.get_redis", return_value=MagicMock()):
            flash = _build_limiters(
                RatePolicy.from_component(_StubComponent("gemini", "flash", 100, 1500))
            )
            pro = _build_limiters(
                RatePolicy.from_component(_StubComponent("gemini", "pro", 100, 1500))
            )

        assert flash[0] is not None and pro[0] is not None
        assert flash[0]._key != pro[0]._key

    def test_rpm_none_skips_rpm_limiter(self) -> None:
        """``rpm=None`` なら ``rpm_limiter`` は ``None`` で返る。"""
        from app.analysis._limiter_factory import _build_limiters

        with patch("app.analysis._limiter_factory.get_redis", return_value=MagicMock()):
            rpm, rpd = _build_limiters(
                RatePolicy.from_component(_StubComponent("gemini", "m", None, 1500))
            )

        assert rpm is None
        assert rpd is not None

    def test_rpd_none_skips_rpd_limiter(self) -> None:
        """``rpd=None`` なら ``rpd_limiter`` は ``None`` で返る。"""
        from app.analysis._limiter_factory import _build_limiters

        with patch("app.analysis._limiter_factory.get_redis", return_value=MagicMock()):
            rpm, rpd = _build_limiters(
                RatePolicy.from_component(_StubComponent("gemini", "m", 100, None))
            )

        assert rpm is not None
        assert rpd is None


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

        policy = RatePolicy.from_component(StubClass())  # type: ignore[arg-type]

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


# 後方互換確認: 既存の単純な _StubComponent は SimpleNamespace でも同等に動く
def test_simplenamespace_also_works_as_source() -> None:
    """SimpleNamespace で属性を生やしたものも duck-typed で受け入れる。"""
    component = SimpleNamespace(PROVIDER="gemini", MODEL="m", RPM=100, RPD=1500)
    policy = RatePolicy.from_component(component)
    assert policy.rpm_key == "ratelimit:gemini:m:rpm"
