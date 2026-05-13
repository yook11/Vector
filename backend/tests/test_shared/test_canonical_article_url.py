"""CanonicalArticleUrl 値オブジェクトのテスト。"""

import json

import pytest
from pydantic import BaseModel, ValidationError

from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl
from app.shared.value_objects.safe_url import SafeUrl


class TestCanonicalArticleUrlNormalization:
    """canonicalize_url の 5 項目が型構築時点で適用されることを保証する。"""

    def test_lowercases_host(self) -> None:
        url = CanonicalArticleUrl("https://Example.COM/foo")
        assert url.root == "https://example.com/foo"

    def test_strips_tracking_params(self) -> None:
        url = CanonicalArticleUrl(
            "https://example.com/foo?utm_source=rss&utm_medium=email&q=keep"
        )
        assert url.root == "https://example.com/foo?q=keep"

    def test_strips_all_known_tracking_params(self) -> None:
        url = CanonicalArticleUrl(
            "https://example.com/foo?fbclid=abc&gclid=def&mc_cid=xyz"
        )
        assert url.root == "https://example.com/foo"

    def test_strips_trailing_slash_on_non_root_path(self) -> None:
        url = CanonicalArticleUrl("https://example.com/foo/")
        assert url.root == "https://example.com/foo"

    def test_keeps_root_path_slash(self) -> None:
        url = CanonicalArticleUrl("https://example.com/")
        assert url.root == "https://example.com/"

    def test_removes_fragment(self) -> None:
        url = CanonicalArticleUrl("https://example.com/foo#section")
        assert url.root == "https://example.com/foo"

    def test_preserves_scheme(self) -> None:
        http = CanonicalArticleUrl("http://example.com/foo")
        https = CanonicalArticleUrl("https://example.com/foo")
        assert http.root == "http://example.com/foo"
        assert https.root == "https://example.com/foo"
        assert http != https


class TestCanonicalArticleUrlIdempotent:
    """`canonical(canonical(x)) == canonical(x)` であること。"""

    def test_str_idempotent(self) -> None:
        once = CanonicalArticleUrl("https://Example.com/foo/?utm_source=rss#main")
        twice = CanonicalArticleUrl(str(once))
        assert once == twice

    def test_accepts_own_instance(self) -> None:
        once = CanonicalArticleUrl("https://example.com/foo")
        twice = CanonicalArticleUrl(once)
        assert once == twice
        assert twice.root == "https://example.com/foo"


class TestCanonicalArticleUrlAcceptsSafeUrlInput:
    """SafeUrl インスタンスを入力として受け、canonical 化して保持する。

    Fetcher 群が `source_url=SafeUrl(link)` のままで動作するための互換。
    """

    def test_accepts_safe_url_and_normalizes(self) -> None:
        raw = SafeUrl("https://Example.com/foo/?utm_source=rss#main")
        canonical = CanonicalArticleUrl(raw)
        assert canonical.root == "https://example.com/foo"

    def test_accepts_already_canonical_safe_url(self) -> None:
        raw = SafeUrl("https://example.com/foo")
        canonical = CanonicalArticleUrl(raw)
        assert canonical.root == "https://example.com/foo"


class TestCanonicalArticleUrlRejectsInvalidInput:
    """SafeUrl invariant (構文 + SSRF) を canonical 値で再検証する。"""

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            CanonicalArticleUrl("")

    def test_rejects_non_http_scheme(self) -> None:
        with pytest.raises(ValidationError):
            CanonicalArticleUrl("ftp://example.com/foo")

    def test_rejects_private_ip(self) -> None:
        with pytest.raises(ValidationError):
            CanonicalArticleUrl("http://127.0.0.1/admin")

    def test_rejects_non_string_non_url_type(self) -> None:
        with pytest.raises(ValidationError):
            CanonicalArticleUrl(123)  # type: ignore[arg-type]


class TestCanonicalArticleUrlBridges:
    """SafeUrl 境界 (extractor.fetch 等) への橋渡し。"""

    def test_as_safe_url_returns_safe_url(self) -> None:
        canonical = CanonicalArticleUrl("https://example.com/foo")
        safe = canonical.as_safe_url()
        assert isinstance(safe, SafeUrl)
        assert safe.root == "https://example.com/foo"

    def test_str_returns_canonical(self) -> None:
        canonical = CanonicalArticleUrl("https://Example.com/foo/?utm_source=rss")
        assert str(canonical) == "https://example.com/foo"

    def test_repr_includes_canonical(self) -> None:
        canonical = CanonicalArticleUrl("https://example.com/foo")
        assert repr(canonical) == "CanonicalArticleUrl('https://example.com/foo')"


class TestCanonicalArticleUrlEqualityAndHashing:
    def test_equal_when_canonical_matches(self) -> None:
        a = CanonicalArticleUrl("https://Example.com/foo/?utm_source=rss")
        b = CanonicalArticleUrl("https://example.com/foo")
        assert a == b
        assert hash(a) == hash(b)
        assert len({a, b}) == 1

    def test_not_equal_to_safe_url(self) -> None:
        canonical = CanonicalArticleUrl("https://example.com/foo")
        safe = SafeUrl("https://example.com/foo")
        assert canonical != safe

    def test_immutable(self) -> None:
        url = CanonicalArticleUrl("https://example.com/foo")
        with pytest.raises(ValidationError, match="frozen"):
            url.root = "https://hacked.com"  # type: ignore[misc]


class TestPydanticIntegration:
    class SampleModel(BaseModel):
        url: CanonicalArticleUrl

    def test_model_from_str(self) -> None:
        m = self.SampleModel(url="https://Example.com/foo/?utm_source=rss")
        assert isinstance(m.url, CanonicalArticleUrl)
        assert m.url.root == "https://example.com/foo"

    def test_model_from_value_object(self) -> None:
        canonical = CanonicalArticleUrl("https://example.com/foo")
        m = self.SampleModel(url=canonical)
        assert isinstance(m.url, CanonicalArticleUrl)
        assert m.url == canonical

    def test_model_dump_unwraps_to_str(self) -> None:
        m = self.SampleModel(url="https://example.com/foo")
        data = m.model_dump()
        assert data == {"url": "https://example.com/foo"}
        assert isinstance(data["url"], str)

    def test_model_dump_json(self) -> None:
        m = self.SampleModel(url="https://example.com/foo")
        data = json.loads(m.model_dump_json())
        assert data["url"] == "https://example.com/foo"
