"""ArticleUrl 値オブジェクトのテスト。"""

import json

import pytest
from pydantic import BaseModel, ValidationError

from app.collection.domain.article_url import (
    ArticleUrl,
    ArticleUrlInvalidError,
)
from app.shared.web_url import (
    WebUrl,
    WebUrlInvalidError,
    WebUrlInvalidReason,
)


class TestArticleUrlNormalization:
    """記事 URL の規則が型構築時点で適用される。"""

    def test_lowercases_host(self) -> None:
        url = ArticleUrl("https://Example.COM/foo")
        assert url.root == "https://example.com/foo"

    def test_preserves_path_case(self) -> None:
        # path は case-sensitive、host のみを小文字化する
        url = ArticleUrl("https://Example.COM/Article/Path")
        assert url.root == "https://example.com/Article/Path"

    def test_strips_tracking_params(self) -> None:
        url = ArticleUrl(
            "https://example.com/foo?utm_source=rss&utm_medium=email&q=keep"
        )
        assert url.root == "https://example.com/foo?q=keep"

    def test_strips_click_ids(self) -> None:
        url = ArticleUrl(
            "https://example.com/a?fbclid=AB&gclid=CD&dclid=EF&msclkid=GH&id=1"
        )
        assert url.root == "https://example.com/a?id=1"

    def test_strips_mailchimp_and_ref(self) -> None:
        url = ArticleUrl(
            "https://example.com/a?mc_cid=AA&mc_eid=BB&ref=cc&ref_src=dd&referrer=ee&q=v"
        )
        assert url.root == "https://example.com/a?q=v"

    def test_keeps_non_tracking_query_in_order(self) -> None:
        # 記事 ID 等の必須 query は順序を保ったまま保持する
        url = ArticleUrl("https://example.com/a?id=42&page=2")
        assert url.root == "https://example.com/a?id=42&page=2"

    def test_keeps_trailing_slash(self) -> None:
        # 補完はこの値で本文を取りに行くため、サイトが正とする末尾の / を残す
        url = ArticleUrl("https://example.com/foo/")
        assert url.root == "https://example.com/foo/"

    def test_trailing_slash_makes_distinct_url(self) -> None:
        assert ArticleUrl("https://example.com/foo") != ArticleUrl(
            "https://example.com/foo/"
        )

    def test_keeps_root_path_slash(self) -> None:
        url = ArticleUrl("https://example.com/")
        assert url.root == "https://example.com/"

    def test_root_only_url_gets_root_slash(self) -> None:
        url = ArticleUrl("https://example.com")
        assert url.root == "https://example.com/"

    def test_omits_default_port(self) -> None:
        url = ArticleUrl("https://example.com:443/a")
        assert url.root == "https://example.com/a"

    def test_builds_from_pydantic_parsed_host(self) -> None:
        # バックスラッシュは pydantic の解釈どおり path の区切りとして扱う
        url = ArticleUrl("http://127.0.0.1\\@evil.example/a/")
        assert url.root == "http://127.0.0.1/@evil.example/a/"

    def test_removes_fragment(self) -> None:
        url = ArticleUrl("https://example.com/foo#section")
        assert url.root == "https://example.com/foo"

    def test_preserves_scheme(self) -> None:
        http = ArticleUrl("http://example.com/foo")
        https = ArticleUrl("https://example.com/foo")
        assert http.root == "http://example.com/foo"
        assert https.root == "https://example.com/foo"
        assert http != https

    def test_combined_transformation(self) -> None:
        url = ArticleUrl("HTTPS://Example.COM/Article/?utm_source=x&id=42#section")
        assert url.root == "https://example.com/Article/?id=42"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # 全 tracking のみの query → query 部丸ごと消える
            (
                "https://example.com/a?utm_source=x&fbclid=y",
                "https://example.com/a",
            ),
            # 末尾 / + fragment + tracking
            (
                "https://example.com/a/?utm_source=x#top",
                "https://example.com/a/",
            ),
            # 値のない query も保持する
            (
                "https://example.com/a?id=&q=v",
                "https://example.com/a?id=&q=v",
            ),
        ],
    )
    def test_edge_cases(self, raw: str, expected: str) -> None:
        assert ArticleUrl(raw).root == expected


class TestArticleUrlIdempotent:
    """`article(article(x)) == article(x)` であること。"""

    @pytest.mark.parametrize(
        "raw",
        [
            "https://example.com/article",
            "https://example.com/",
            "http://example.com/path?id=1",
            "HTTPS://EXAMPLE.com/A/?utm_source=x#frag",
            "https://example.com",
            "http://127.0.0.1\\@evil.example/a/",
        ],
    )
    def test_str_idempotent(self, raw: str) -> None:
        once = ArticleUrl(raw)
        assert ArticleUrl(str(once)) == once


class TestArticleUrlRejectsInvalidInput:
    """WebUrl の形式の不変条件を記事 URL の値で再検証する。"""

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            ArticleUrl("")

    def test_rejects_non_http_scheme(self) -> None:
        with pytest.raises(ValidationError):
            ArticleUrl("ftp://example.com/foo")

    def test_rejects_non_string_non_url_type(self) -> None:
        with pytest.raises(ValidationError):
            ArticleUrl(123)  # type: ignore[arg-type]


class TestArticleUrlFromRaw:
    """``from_raw`` は失敗理由を型 (reason) で運ぶ factory。

    reason の正本テスト: ``__cause__`` 連鎖が pydantic 非経由で保たれることも確かめる。
    """

    def test_from_raw_normalizes_on_success(self) -> None:
        url = ArticleUrl.from_raw("https://Example.com/foo/?utm_source=rss#main")
        assert url.root == "https://example.com/foo/"

    @pytest.mark.parametrize(
        ("raw", "expected_reason"),
        [
            ("", WebUrlInvalidReason.URL_EMPTY),
            ("ftp://example.com/foo", WebUrlInvalidReason.URL_NOT_HTTP),
            ("example.com/foo", WebUrlInvalidReason.URL_NOT_HTTP),
            (
                "https://example.com/" + "a" * (2049 - len("https://example.com/")),
                WebUrlInvalidReason.URL_TOO_LONG,
            ),
            # 入力の長さは追跡用パラメータを除く前に確かめる
            pytest.param(
                "https://example.com/a?utm_source=" + "x" * 2040,
                WebUrlInvalidReason.URL_TOO_LONG,
                id="too-long-before-tracking-removal",
            ),
        ],
    )
    def test_from_raw_classifies_reason(
        self, raw: str, expected_reason: WebUrlInvalidReason
    ) -> None:
        with pytest.raises(ArticleUrlInvalidError) as exc_info:
            ArticleUrl.from_raw(raw)
        assert exc_info.value.reason is expected_reason

    def test_from_raw_chains_web_url_invalid_error(self) -> None:
        # __cause__ に WebUrlInvalidError が残り、監査 error_chain で系統が辿れる
        with pytest.raises(ArticleUrlInvalidError) as exc_info:
            ArticleUrl.from_raw("ftp://example.com")
        cause = exc_info.value.__cause__
        assert isinstance(cause, WebUrlInvalidError)
        assert cause.reason is WebUrlInvalidReason.URL_NOT_HTTP


class TestArticleUrlBridges:
    """WebUrl 境界 (記事の取得処理等) への橋渡し。"""

    def test_as_web_url_returns_web_url(self) -> None:
        article_url = ArticleUrl("https://example.com/foo")
        web_url = article_url.as_web_url()
        assert isinstance(web_url, WebUrl)
        assert web_url.root == "https://example.com/foo"

    def test_str_returns_normalized_value(self) -> None:
        article_url = ArticleUrl("https://Example.com/foo/?utm_source=rss")
        assert str(article_url) == "https://example.com/foo/"

    def test_repr_includes_value(self) -> None:
        article_url = ArticleUrl("https://example.com/foo")
        assert repr(article_url) == "ArticleUrl('https://example.com/foo')"


class TestArticleUrlEqualityAndHashing:
    def test_equal_when_normalized_value_matches(self) -> None:
        a = ArticleUrl("https://Example.com/foo/?utm_source=rss")
        b = ArticleUrl("https://example.com/foo/")
        assert a == b
        assert hash(a) == hash(b)
        assert len({a, b}) == 1

    def test_not_equal_to_web_url(self) -> None:
        article_url = ArticleUrl("https://example.com/foo")
        web_url = WebUrl("https://example.com/foo")
        assert article_url != web_url

    def test_immutable(self) -> None:
        url = ArticleUrl("https://example.com/foo")
        with pytest.raises(ValidationError, match="frozen"):
            url.root = "https://hacked.com"  # type: ignore[misc]


class TestPydanticIntegration:
    class SampleModel(BaseModel):
        url: ArticleUrl

    def test_model_from_str(self) -> None:
        m = self.SampleModel(url="https://Example.com/foo/?utm_source=rss")
        assert isinstance(m.url, ArticleUrl)
        assert m.url.root == "https://example.com/foo/"

    def test_model_from_value_object(self) -> None:
        article_url = ArticleUrl("https://example.com/foo")
        m = self.SampleModel(url=article_url)
        assert isinstance(m.url, ArticleUrl)
        assert m.url == article_url

    def test_model_dump_unwraps_to_str(self) -> None:
        m = self.SampleModel(url="https://example.com/foo")
        data = m.model_dump()
        assert data == {"url": "https://example.com/foo"}
        assert isinstance(data["url"], str)

    def test_model_dump_json(self) -> None:
        m = self.SampleModel(url="https://example.com/foo")
        data = json.loads(m.model_dump_json())
        assert data["url"] == "https://example.com/foo"
