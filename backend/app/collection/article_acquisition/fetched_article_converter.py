"""``FetchedArticle`` → 「何ができたか」への変換 (純粋関数)。

Ready 昇格 / Observed 保留の最終分岐を ``convert_fetched_article`` に集約する。
"""

from __future__ import annotations

from app.collection.article_acquisition.fetched_article import FetchedArticle
from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.observed_article import ObservedArticle
from app.collection.domain.value_objects import PublishedAt
from app.collection.sources.source_metadata import SourceMetadata
from app.log_policy.policies.article_fetch import ARTICLE_FETCH_LOG_RULES
from app.log_policy.runtime import create_policy_json_logger

logger = create_policy_json_logger(__name__, ARTICLE_FETCH_LOG_RULES)


def convert_fetched_article(
    fetched: FetchedArticle,
    *,
    source: SourceMetadata,
    source_id: int,
) -> AnalyzableArticle | ObservedArticle:
    """1 ``FetchedArticle`` を「何ができたか」に変換する。

    Returns:
        ``AnalyzableArticle`` — body + published_at が揃い品質ゲート通過。
        ``ObservedArticle`` — title + URL は揃うが Ready 不成立 (取れた事実を
        全保存)。
    """
    source_name = source.name
    origin = source.observed_origin

    published_at = PublishedAt.from_datetime(fetched.published_at)

    if not source.completion_policy.requires_html_completion():
        article = AnalyzableArticle.try_build(
            title=fetched.title,
            body=fetched.body,
            published_at=published_at,
            source_id=source_id,
            source_url=fetched.url,
        )
        if article is not None:
            logger.info(
                "fetched_article_converted",
                article_type="analyzable",
                source_name=str(source_name),
                body_length=len(article.body),
            )
            return article

    observed = ObservedArticle.build(
        source_name=source_name,
        source_url=fetched.url,
        title=fetched.title,
        body=fetched.body,
        published_at=published_at,
        origin=origin,
    )
    logger.info(
        "fetched_article_converted",
        article_type="observed",
        source_name=str(source_name),
        has_title=observed.title is not None,
        title_origin=str(observed.title.origin) if observed.title else None,
        has_body=observed.body is not None,
        body_origin=str(observed.body.origin) if observed.body else None,
        body_length=len(observed.body.value) if observed.body else None,
        has_published_at=observed.published_at is not None,
        published_at_origin=(
            str(observed.published_at.origin) if observed.published_at else None
        ),
    )
    return observed
