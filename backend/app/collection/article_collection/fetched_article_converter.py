"""``FetchedArticle`` → ``AnalyzableArticle | ObservedArticle`` 変換 (純粋関数)。

per-source 責務は body / published を信用できる形で渡せるかのみ。Ready 昇格 /
Observed 保留 / 変換不能 の最終分岐を ``convert_fetched_article`` に集約する。
変換不能 entry は ``None`` を返さず ``FetchedArticleConversionError`` を raise
する (stream を止めないための値化は ``ArticleFetcher`` の責務)。

``AnalyzableArticle`` 不成立は想定内の正常系 (Ready 候補 ⊆ Observed 候補)。
precondition (title / URL) を満たさない entry のみ変換失敗 = 例外となる。失敗時
の理由判定とログ出力は ``_raise_conversion_failed`` に集約する。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn

import structlog

from app.collection.article_collection.errors import (
    ConversionReason,
    FetchedArticleConversionError,
)
from app.collection.article_collection.fetched_article import FetchedArticle
from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.article_limits import ARTICLE_TITLE_MAX_LENGTH
from app.collection.domain.canonical_article_url import CanonicalArticleUrl
from app.collection.domain.observed_article import ObservedArticle
from app.collection.domain.value_objects import PublishedAt
from app.collection.sources.article_source import ArticleSource
from app.shared.value_objects.source_name import SourceName

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ConversionRejection:
    """stream 境界で変換不能 entry を表す値。

    per-entry raise だと source stream 全体が止まるため、例外を値に落として
    継続させる。原因例外を内包し監査が ``__cause__`` 連鎖を辿れる。
    """

    error: FetchedArticleConversionError


def _raise_conversion_failed(
    *,
    reason: ConversionReason,
    fetched: FetchedArticle,
    source_name: SourceName,
    cause: Exception | None = None,
) -> NoReturn:
    """``FetchedArticleConversionError`` 構築 + ログ出力 + raise の集約点。"""
    has_title = bool(fetched.title)
    body_length = len(fetched.body) if fetched.body is not None else None
    has_published_at = fetched.published_at is not None
    raw_url = fetched.url or None

    err = FetchedArticleConversionError(
        f"conversion rejected: {reason}",
        conversion_reason=reason,
        source_name=str(source_name),
        raw_url=raw_url,
        has_title=has_title,
        body_length=body_length,
        has_published_at=has_published_at,
    )
    logger.info(
        "fetched_article_conversion_failed",
        source_name=str(source_name),
        conversion_reason=str(reason),
        has_title=has_title,
        body_length=body_length,
        has_published_at=has_published_at,
    )
    raise err from cause


def convert_fetched_article(
    fetched: FetchedArticle,
    *,
    source: ArticleSource,
    source_id: int,
) -> AnalyzableArticle | ObservedArticle:
    """1 ``FetchedArticle`` を獲得型に変換する。

    URL / title は獲得型の土台 (identity)。不在 / 不正なら獲得型は成立し得ない
    ため、Phase 0 で precondition として即 raise する。残りの正規化と Ready /
    Observed の構築はこの precondition 通過を前提に進む。

    Returns:
        ``AnalyzableArticle`` — body + published_at が揃い品質ゲート通過。
        ``ObservedArticle`` — title + URL は揃うが Ready 不成立 (取れた事実を
        全保存)。

    Raises:
        FetchedArticleConversionError: title / URL precondition を満たさない entry。
    """
    source_name = source.name
    origin = source.observed_origin

    title = fetched.title.strip()[:ARTICLE_TITLE_MAX_LENGTH]
    if not title:
        _raise_conversion_failed(
            reason=ConversionReason.MISSING_TITLE,
            fetched=fetched,
            source_name=source_name,
        )

    if not fetched.url:
        _raise_conversion_failed(
            reason=ConversionReason.MISSING_URL,
            fetched=fetched,
            source_name=source_name,
        )

    try:
        source_url = CanonicalArticleUrl(fetched.url)
    except ValueError as err:
        _raise_conversion_failed(
            reason=ConversionReason.INVALID_URL,
            fetched=fetched,
            source_name=source_name,
            cause=err,
        )

    published_at = PublishedAt.from_datetime(fetched.published_at)

    if not source.completion_policy.requires_html_completion():
        article = AnalyzableArticle.try_build(
            title=title,
            body=fetched.body,
            published_at=published_at,
            source_id=source_id,
            source_url=source_url,
        )
        if article is not None:
            logger.info(
                "fetched_article_converted",
                type="analyzable",
                source_name=str(source_name),
                body_length=len(article.body),
            )
            return article

    observed = ObservedArticle.build(
        source_name=source_name,
        source_url=source_url,
        title=title,
        body=fetched.body,
        published_at=published_at,
        origin=origin,
    )
    logger.info(
        "fetched_article_converted",
        type="observed",
        source_name=str(source_name),
        **observed.to_audit_fields(),
    )
    return observed
