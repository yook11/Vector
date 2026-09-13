"""観測値とHTML素材を統合し、完成記事または新経路用の構築拒否を伝える。"""

from __future__ import annotations

from app.collection.article_completion.content import ScrapedContent
from app.collection.article_completion.errors import ArticleCompletionRejectedError
from app.collection.domain.analyzable_article import (
    AnalyzableArticle,
    QualityTooLow,
)
from app.collection.domain.canonical_article_url import CanonicalArticleUrl
from app.collection.domain.observed_article import ObservedArticle
from app.collection.sources.article_completion_policy import ArticleCompletionPolicy


def complete_with_html(
    observed: ObservedArticle,
    completion_policy: ArticleCompletionPolicy,
    html: ScrapedContent,
    *,
    source_id: int,
    source_url: CanonicalArticleUrl,
) -> AnalyzableArticle:
    """ScrapedContent を観測値と merge し AnalyzableArticle に昇格する。"""
    obs_title = observed.title.value if observed.title is not None else None
    obs_body = observed.body.value if observed.body is not None else None
    obs_pub = observed.published_at.value if observed.published_at is not None else None

    resolved = completion_policy.resolve(
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
        raise ArticleCompletionRejectedError(
            defects=built.defects, unmapped=built.unmapped
        )
    return built
