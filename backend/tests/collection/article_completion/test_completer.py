"""``ArticleHtmlCompleter`` の契約テスト — 純粋 sync アダプタの出力型保証。

検証する不変条件 (副作用ゼロ。DB も network も触らず ``ScrapedContent`` を受けて
``AnalyzableArticle | CompletionRejection`` の閉じ union に必ず収まる):

- ``ScrapedContent`` + promotion 成功 → ``AnalyzableArticle``
- ``ScrapedContent`` + promotion 失敗 → ``CompletionRejection``

取得 (scrape) は service が先に済ませ、成功した ``ScrapedContent`` だけが
completer に渡る。transport / scrape 失敗の値化は scraper / service の責務で、
本テストの対象外 (``test_scraper.py`` / ``test_service.py`` が所有)。completer は
profile を知らず ``ReadyForArticleCompletion`` 経由で受け取り ``complete_with_html``
(純粋関数) に委譲する。
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.collection.article_completion.completer import ArticleHtmlCompleter
from app.collection.article_completion.completion_failure import (
    CompletionRejection,
)
from app.collection.article_completion.ready import ReadyForArticleCompletion
from app.collection.article_completion.scraper import ScrapedContent
from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.canonical_article_url import CanonicalArticleUrl
from app.collection.domain.observed_article import (
    ObservedArticle,
    ObservedField,
    ObservedOrigin,
)
from app.collection.domain.value_objects import PublishedAt
from app.collection.sources.article_completion_policy import DEFAULT_POLICY
from app.collection.sources.source_name import SourceName

_URL = CanonicalArticleUrl("https://example.com/article")


def _ready(
    *,
    observed_published: PublishedAt | None = PublishedAt(
        value=datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)
    ),
) -> ReadyForArticleCompletion:
    return ReadyForArticleCompletion(
        pending_id=1,
        source_id=1,
        attempt_count=1,
        observed=ObservedArticle(
            source_name=SourceName("Feed Source"),
            source_url=_URL,
            title=ObservedField(value="Feed Title", origin=ObservedOrigin.feed),
            published_at=(
                ObservedField(value=observed_published, origin=ObservedOrigin.feed)
                if observed_published is not None
                else None
            ),
        ),
        profile=DEFAULT_POLICY,
        source_url=_URL,
    )


def test_scraped_content_success_returns_analyzable_article() -> None:
    """``ScrapedContent`` + promotion 成功 → ``AnalyzableArticle``。"""
    content = ScrapedContent(
        title="HTML Title",
        body="x" * 200,
        published_at=PublishedAt(value=datetime(2026, 5, 1, tzinfo=UTC)),
    )
    result = ArticleHtmlCompleter().complete(_ready(), content)

    assert isinstance(result, AnalyzableArticle)


def test_promotion_failure_returns_completion_rejection() -> None:
    """published_at が観測 / HTML 両方欠落 → 必須 Field 違反として
    ``CompletionRejection`` に畳む。"""
    content = ScrapedContent(title="OK", body="x" * 200, published_at=None)
    result = ArticleHtmlCompleter().complete(_ready(observed_published=None), content)

    assert isinstance(result, CompletionRejection)
    assert result.reason_code == "completion_invariant_rejected"
    assert result.detail is not None
    assert "published_at" in result.detail
