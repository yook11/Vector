"""項目名から処理を選ぶサニタイズ共通入口と、記事URLを調査に使える形へ絞る処理の契約。"""

from __future__ import annotations

import pytest

from app.log_policy.sanitize import sanitize_article_url, sanitize_field_value

pytestmark = pytest.mark.unit


class TestFieldSanitization:
    """共通入口は登録項目に対応する処理を呼び、入力型が合わなければ固定マーカーを返す。"""

    @pytest.mark.parametrize("field_name", ["canonical_url", "source_url"])
    def test_article_url_field_returns_article_url_sanitization_result(
        self, field_name: str
    ) -> None:
        """記事URLの項目に文字列を渡すと、記事URLのサニタイズで処理した値を返す。"""
        result = sanitize_field_value(field_name, "https://example.com/a/1?p=123#top")

        assert result == "https://example.com/a/1?p=123"

    def test_field_with_wrong_type_returns_unsupported(self) -> None:
        """文字列を期待する項目に数値を渡すと、[unsupported]を返す。"""
        result = sanitize_field_value("canonical_url", 123)

        assert result == "[unsupported]"


class TestArticleUrl:
    """記事URLは記事の特定に使う部分を残し、userinfoとフラグメントを落とす。"""

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            pytest.param(
                "https://user:synthetic@example.com/a/1?p=123#top",
                "https://example.com/a/1?p=123",
                id="userinfo_and_fragment",
            ),
            pytest.param(
                "https://user@example.com/a/1",
                "https://example.com/a/1",
                id="userinfo_without_password",
            ),
            pytest.param(
                "https://example.com/a/1#section-2",
                "https://example.com/a/1",
                id="fragment",
            ),
            pytest.param(
                "https://example.com/a/1?p=123#",
                "https://example.com/a/1?p=123",
                id="empty_fragment",
            ),
        ],
    )
    def test_userinfo_and_fragment_are_removed(self, url: str, expected: str) -> None:
        """userinfoとフラグメントを落とし、scheme・host・port・path・クエリを残す。"""
        assert sanitize_article_url(url) == expected

    @pytest.mark.parametrize(
        "url",
        [
            pytest.param("https://example.com/a/1?p=123", id="query"),
            pytest.param("http://Example.com:8080/a/1", id="scheme_host_port"),
            pytest.param("https://medium.com/@author/post-title", id="at_sign_in_path"),
            pytest.param(
                "https://example.com/a/1?utm_source=feed", id="tracking_parameter"
            ),
        ],
    )
    def test_parts_used_to_identify_article_are_preserved(self, url: str) -> None:
        """記事の特定に使う部分だけの値は変えず、追跡用パラメーターの除去は発生元に任せる。"""
        assert sanitize_article_url(url) == url

    def test_value_that_cannot_be_split_as_url_is_unsupported(self) -> None:
        """URLとして分解できない値は、残す部分を決められないため[unsupported]にする。"""
        assert sanitize_article_url("http://[::1/a/1") == "[unsupported]"
