"""``FetchedArticle`` / ``FetchOutcome`` の invariant テスト。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.collection.extraction.domain.value_objects import PublishedAt
from app.collection.ingestion.domain.fetched_article import (
    Failed,
    FailureReason,
    FetchedArticle,
    Ready,
)
from app.shared.value_objects.safe_url import SafeUrl


def _published_at() -> PublishedAt:
    return PublishedAt(value=datetime(2026, 4, 30, 0, 0, 0, tzinfo=UTC))


def _safe_url(url: str = "https://example.com/article") -> SafeUrl:
    return SafeUrl(url)


class TestFetchedArticle:
    def test_accepts_minimal_valid_input(self) -> None:
        article = FetchedArticle(
            title="Test",
            body="x" * 50,
            published_at=_published_at(),
            source_id=1,
            source_url=_safe_url(),
        )
        assert article.title == "Test"
        assert article.source_id == 1

    def test_rejects_empty_title(self) -> None:
        with pytest.raises(ValueError):
            FetchedArticle(
                title="",
                body="x" * 50,
                published_at=_published_at(),
                source_id=1,
                source_url=_safe_url(),
            )

    def test_rejects_title_over_500_chars(self) -> None:
        with pytest.raises(ValueError):
            FetchedArticle(
                title="x" * 501,
                body="x" * 50,
                published_at=_published_at(),
                source_id=1,
                source_url=_safe_url(),
            )

    def test_rejects_body_under_50_chars(self) -> None:
        with pytest.raises(ValueError):
            FetchedArticle(
                title="Test",
                body="x" * 49,
                published_at=_published_at(),
                source_id=1,
                source_url=_safe_url(),
            )

    def test_rejects_non_positive_source_id(self) -> None:
        with pytest.raises(ValueError):
            FetchedArticle(
                title="Test",
                body="x" * 50,
                published_at=_published_at(),
                source_id=0,
                source_url=_safe_url(),
            )

    def test_is_frozen(self) -> None:
        article = FetchedArticle(
            title="Test",
            body="x" * 50,
            published_at=_published_at(),
            source_id=1,
            source_url=_safe_url(),
        )
        with pytest.raises(ValueError):
            article.title = "Changed"  # type: ignore[misc]


class TestFailureReason:
    def test_accepts_valid_code(self) -> None:
        reason = FailureReason(code="http_transient", retryable=True)
        assert reason.code == "http_transient"
        assert reason.retryable is True
        assert reason.detail is None

    def test_accepts_detail(self) -> None:
        reason = FailureReason(
            code="published_at_missing",
            retryable=False,
            detail="rss_pubdate_missing",
        )
        assert reason.detail == "rss_pubdate_missing"

    def test_is_frozen(self) -> None:
        reason = FailureReason(code="http_transient", retryable=True)
        with pytest.raises(ValueError):
            reason.code = "http_blocked"  # type: ignore[misc]


class TestFetchOutcome:
    def test_ready_carries_article(self) -> None:
        article = FetchedArticle(
            title="Test",
            body="x" * 50,
            published_at=_published_at(),
            source_id=1,
            source_url=_safe_url(),
        )
        outcome = Ready(article=article)
        assert isinstance(outcome, Ready)
        assert outcome.article is article

    def test_failed_carries_reason(self) -> None:
        reason = FailureReason(code="extraction_empty", retryable=False)
        outcome = Failed(reason=reason)
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "extraction_empty"

    def test_match_dispatch(self) -> None:
        """Union 型を ``match`` で分岐できる (上流 Service の典型用法)。"""
        article = FetchedArticle(
            title="Test",
            body="x" * 50,
            published_at=_published_at(),
            source_id=1,
            source_url=_safe_url(),
        )
        outcomes = [
            Ready(article=article),
            Failed(reason=FailureReason(code="paywalled", retryable=False)),
        ]
        ready_count = 0
        failed_codes: list[str] = []
        for outcome in outcomes:
            match outcome:
                case Ready(article=a):
                    ready_count += 1
                    assert a.title == "Test"
                case Failed(reason=r):
                    failed_codes.append(r.code)
        assert ready_count == 1
        assert failed_codes == ["paywalled"]
