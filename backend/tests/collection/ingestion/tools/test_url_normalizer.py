"""``normalize_article_url`` のユニットテスト。"""

from __future__ import annotations

from app.collection.ingestion.tools.url_normalizer import normalize_article_url
from app.shared.value_objects.safe_url import SafeUrl


def _norm(raw: str) -> str:
    return str(normalize_article_url(SafeUrl(raw)))


class TestNormalizeArticleUrl:
    def test_returns_same_object_when_no_query(self) -> None:
        original = SafeUrl("https://example.com/article")
        result = normalize_article_url(original)
        assert result is original

    def test_strips_utm_params(self) -> None:
        result = _norm(
            "https://example.com/article?utm_source=twitter&utm_medium=social"
            "&utm_campaign=launch"
        )
        assert result == "https://example.com/article"

    def test_strips_gclid_and_fbclid(self) -> None:
        result = _norm("https://example.com/article?gclid=abc&fbclid=xyz")
        assert result == "https://example.com/article"

    def test_preserves_non_tracking_params(self) -> None:
        result = _norm("https://example.com/article?id=42&utm_source=twitter&page=2")
        assert result == "https://example.com/article?id=42&page=2"

    def test_returns_same_object_when_no_tracking_params(self) -> None:
        original = SafeUrl("https://example.com/article?id=42&page=2")
        result = normalize_article_url(original)
        assert result is original

    def test_preserves_fragment(self) -> None:
        result = _norm("https://example.com/article?utm_source=x#section-2")
        assert result == "https://example.com/article#section-2"

    def test_preserves_trailing_slash(self) -> None:
        result = _norm("https://example.com/article/?utm_source=x")
        assert result == "https://example.com/article/"

    def test_case_insensitive_param_match(self) -> None:
        result = _norm("https://example.com/article?UTM_SOURCE=x&FBCLID=y&id=42")
        assert result == "https://example.com/article?id=42"

    def test_strips_mailchimp_params(self) -> None:
        result = _norm("https://example.com/article?mc_cid=abc&mc_eid=xyz")
        assert result == "https://example.com/article"

    def test_strips_ref_params(self) -> None:
        result = _norm("https://example.com/article?ref=newsletter&ref_src=email&id=1")
        assert result == "https://example.com/article?id=1"
