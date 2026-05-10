"""``_build_limiters`` の役割別キー独立性テスト (構造的保証)。

同一モデルを異なる役割 (extract / classify / embed) で使ってもレート制御
カウンターが Redis 上で共有されないことを Redis キー命名規約で確認する。
"""

from unittest.mock import MagicMock, patch


class TestBuildLimitersKeyIsolation:
    """同一モデルを異なる役割で使ってもレート制御カウンターが共有されないこと。"""

    def test_keys_isolated_by_role(self) -> None:
        """extract と classify で同じモデルでも Redis キーが独立する。"""
        from app.analysis._limiter_factory import _build_limiters

        with patch("app.redis.get_redis", return_value=MagicMock()):
            extract_rpm, extract_rpd = _build_limiters(
                "extract", "shared-model", 100, 1500
            )
            classify_rpm, classify_rpd = _build_limiters(
                "classify", "shared-model", 100, 1500
            )

        assert extract_rpm is not None
        assert extract_rpd is not None
        assert classify_rpm is not None
        assert classify_rpd is not None

        assert extract_rpm._key != classify_rpm._key
        assert extract_rpd._key != classify_rpd._key
        assert "extract" in extract_rpd._key
        assert "classify" in classify_rpd._key

    def test_embed_role_key_distinct(self) -> None:
        """embed 役割のキーも他と独立する。"""
        from app.analysis._limiter_factory import _build_limiters

        with patch("app.redis.get_redis", return_value=MagicMock()):
            extract_rpm, _ = _build_limiters("extract", "m", 60, None)
            embed_rpm, _ = _build_limiters("embed", "m", 60, None)

        assert extract_rpm is not None
        assert embed_rpm is not None
        assert extract_rpm._key != embed_rpm._key
        assert "embed" in embed_rpm._key
