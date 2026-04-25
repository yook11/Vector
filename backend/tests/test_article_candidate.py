"""ArticleCandidate.from_external のユニットテスト。"""

from app.collection.ingestion.domain import ArticleCandidate
from app.shared.value_objects.safe_url import SafeUrl


def test_from_external_with_valid_input() -> None:
    candidate = ArticleCandidate.from_external(
        raw_url="https://example.com/article", raw_title="Hello"
    )
    assert candidate is not None
    assert isinstance(candidate.url, SafeUrl)
    assert candidate.title == "Hello"


def test_from_external_rejects_unsafe_url() -> None:
    candidate = ArticleCandidate.from_external(
        raw_url="javascript:alert(1)", raw_title="Hello"
    )
    assert candidate is None


def test_from_external_rejects_empty_url() -> None:
    candidate = ArticleCandidate.from_external(raw_url="", raw_title="Hello")
    assert candidate is None


def test_from_external_rejects_empty_title() -> None:
    candidate = ArticleCandidate.from_external(
        raw_url="https://example.com/a", raw_title=""
    )
    assert candidate is None


def test_from_external_strips_html_tags_from_title() -> None:
    candidate = ArticleCandidate.from_external(
        raw_url="https://example.com/a", raw_title="<b>Bold</b> title"
    )
    assert candidate is not None
    assert candidate.title == "Bold title"


def test_from_external_truncates_long_title() -> None:
    long_title = "x" * 600
    candidate = ArticleCandidate.from_external(
        raw_url="https://example.com/a", raw_title=long_title
    )
    assert candidate is not None
    assert len(candidate.title) == 500
