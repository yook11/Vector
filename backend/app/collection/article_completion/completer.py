"""``ObservedArticle`` を完成形に解決する純粋境界 (副作用なし)。"""

from __future__ import annotations

from app.collection.article_completion.completion_failure import (
    CompletionRejection,
)
from app.collection.article_completion.ready import ReadyForArticleCompletion
from app.collection.article_completion.scraper import ScrapedContent
from app.collection.domain.analyzable_article import (
    AnalyzableArticle,
    QualityTooLow,
)
from app.collection.domain.canonical_article_url import CanonicalArticleUrl
from app.collection.domain.observed_article import ObservedArticle
from app.collection.sources.article_completion_policy import ArticleCompletionPolicy


def complete_with_html(
    observed: ObservedArticle,
    profile: ArticleCompletionPolicy,
    html: ScrapedContent,
    *,
    source_id: int,
    source_url: CanonicalArticleUrl,
) -> AnalyzableArticle | CompletionRejection:
    """ScrapedContent を観測値と merge し AnalyzableArticle に昇格する。"""
    obs_title = observed.title.value if observed.title is not None else None
    obs_body = observed.body.value if observed.body is not None else None
    obs_pub = observed.published_at.value if observed.published_at is not None else None

    resolved = profile.resolve(
        observed_title=obs_title,
        html_title=html.title,
        observed_body=obs_body,
        html_body=html.body,
        observed_published_at=obs_pub,
        html_published_at=html.published_at,
    )

    built = AnalyzableArticle.build_or_reject(
        title=resolved.title,
        body=resolved.body,
        published_at=resolved.published_at,
        source_id=source_id,
        source_url=source_url,
    )
    if isinstance(built, QualityTooLow):
        return CompletionRejection.from_quality_too_low(built)
    return built


class ArticleHtmlCompleter:
    """ScrapedContent から AnalyzableArticle を完成させる薄い adapter。"""

    def complete(
        self, ready: ReadyForArticleCompletion, scraped: ScrapedContent
    ) -> AnalyzableArticle | CompletionRejection:
        """``ready`` の観測値と ``scraped`` を merge し完成 or 構築拒否を返す。"""
        return complete_with_html(
            ready.observed,
            ready.profile,
            scraped,
            source_id=ready.source_id,
            source_url=ready.source_url,
        )
