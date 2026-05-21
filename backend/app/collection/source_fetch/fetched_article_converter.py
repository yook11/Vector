"""``FetchedArticle`` → ``AnalyzableArticle | ObservedArticle`` 変換 (純粋関数)。

per-source 責務は body / published を信用できる形で渡せるかのみ。Ready 昇格 /
Observed 保留 / 変換不能 の最終分岐を ``convert_fetched_article`` に集約する。
変換不能 entry は ``None`` を返さず ``FetchedArticleConversionError`` を raise
する (stream を止めないための値化は ``ArticleFetcher`` の責務)。

``AnalyzableArticle`` 不成立は想定内の正常系 (Ready 候補 ⊆ Observed 候補)。
``ObservedArticle`` にもなれなかった時に初めて変換失敗 = 例外となる。失敗時の
理由判定とログ出力は ``_raise_conversion_failed`` に集約する。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import NoReturn

import structlog

from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.article_limits import (
    ARTICLE_BODY_MAX_LENGTH,
    ARTICLE_BODY_MIN_LENGTH,
    ARTICLE_TITLE_MAX_LENGTH,
)
from app.collection.domain.canonical_article_url import CanonicalArticleUrl
from app.collection.domain.observed_article import ObservedArticle, ObservedField
from app.collection.domain.value_objects import PublishedAt
from app.collection.source_fetch.errors import (
    ConversionReason,
    FetchedArticleConversionError,
)
from app.collection.source_fetch.fetched_article import FetchedArticle
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
    title: str | None,
    title_trimmed: str | None,
    link: str | None,
    source_url: CanonicalArticleUrl | None,
    url_err: Exception | None,
    body_candidate: str | None,
    published_hint: datetime | None,
    observed_err: Exception | None,
    source_name: SourceName,
) -> NoReturn:
    """変換失敗の理由確定 + ログ出力 + ``FetchedArticleConversionError`` raise。

    上から最初の不成立を ``conversion_reason`` として確定する。
    ``raw_url`` はログに出さない (URL query に token 混入の可能性 — 監査側で
    ``redact_secrets`` 経由で永続化される)。``__cause__`` には observed 構築
    失敗 → URL parse 失敗の順で原因例外を連鎖させる。
    """
    if title_trimmed is None:
        conversion_reason = ConversionReason.MISSING_TITLE
    elif not link:
        conversion_reason = ConversionReason.MISSING_URL
    elif source_url is None:
        conversion_reason = ConversionReason.INVALID_URL
    else:
        conversion_reason = ConversionReason.OBSERVED_BUILD_FAILED

    body_length = len(body_candidate) if body_candidate is not None else None
    has_title = title is not None
    has_published_at = published_hint is not None

    err = FetchedArticleConversionError(
        f"conversion rejected: {conversion_reason}",
        conversion_reason=conversion_reason,
        source_name=str(source_name),
        raw_url=link,
        has_title=has_title,
        body_length=body_length,
        has_published_at=has_published_at,
    )
    logger.info(
        "fetched_article_conversion_failed",
        source_name=str(source_name),
        conversion_reason=str(conversion_reason),
        has_title=has_title,
        body_length=body_length,
        has_published_at=has_published_at,
    )
    raise err from (observed_err or url_err)


def convert_fetched_article(
    fetched: FetchedArticle,
    *,
    source: ArticleSource,
    source_id: int,
) -> AnalyzableArticle | ObservedArticle:
    """1 ``FetchedArticle`` を獲得型に変換する。

    空 str / ``None`` の "不在" 表現を正規化してから判定する。獲得型の直接
    構築をこの関数内に閉じ込める。

    Phase 1: 正規化 (raise しない、状態を None-able で集める)
    Phase 2: Ready 候補なら ``AnalyzableArticle`` 構築試行 (失敗は想定内 fall-through)
    Phase 3: Observed 候補なら ``ObservedArticle`` 構築試行
    Phase 4: 両方なれず → ``_raise_conversion_failed`` を 1 回呼ぶ

    Returns:
        ``AnalyzableArticle`` — body + published_at が揃い品質ゲート通過。
        ``ObservedArticle`` — title + URL は揃うが Ready 不成立 (取れた事実を
        全保存)。

    Raises:
        FetchedArticleConversionError: title / URL 無効、または Observed 構築も
            失敗した entry。
    """
    title = fetched.title or None
    link = fetched.url or None
    body_candidate = fetched.body
    published_hint = fetched.published_at
    source_name = source.name
    origin = source.observed_origin
    ready_precluded = source.completion_profile.precludes_stage1_ready()

    # Phase 1: 正規化
    title_trimmed: str | None = None
    if title is not None:
        candidate = title.strip()[:ARTICLE_TITLE_MAX_LENGTH]
        if candidate:
            title_trimmed = candidate

    source_url: CanonicalArticleUrl | None = None
    url_err: Exception | None = None
    if link:
        try:
            source_url = CanonicalArticleUrl(link)
        except ValueError as err:
            url_err = err

    # tz-naive datetime は published として採用しない (PublishedAt が拒否)。
    # 採用できなかった場合は Observed に published_at=None で流す。
    published_at: PublishedAt | None = None
    if published_hint is not None:
        try:
            published_at = PublishedAt(value=published_hint)
        except ValueError:
            published_at = None

    # Phase 2: Ready 候補なら AnalyzableArticle 構築試行 (and chain で narrow)
    if (
        not ready_precluded
        and title_trimmed is not None
        and source_url is not None
        and body_candidate is not None
        and ARTICLE_BODY_MIN_LENGTH <= len(body_candidate) <= ARTICLE_BODY_MAX_LENGTH
        and published_at is not None
    ):
        try:
            article = AnalyzableArticle(
                title=title_trimmed,
                body=body_candidate,
                published_at=published_at,
                source_id=source_id,
                source_url=source_url,
            )
            logger.info(
                "fetched_article_converted",
                type="analyzable",
                source_name=str(source_name),
            )
            return article
        except ValueError:
            # Ready 二次的制約違反 (title sanitize 等の domain 側 invariant) は
            # Observed fallback で救う。想定内の正常系 fall-through。
            pass

    # Phase 3: Observed 候補なら ObservedArticle 構築試行
    observed_err: Exception | None = None
    if title_trimmed is not None and source_url is not None:
        try:
            observed = ObservedArticle(
                source_name=source_name,
                source_url=source_url,
                title=ObservedField(value=title_trimmed, origin=origin),
                body=(
                    ObservedField(value=body_candidate, origin=origin)
                    if body_candidate
                    else None
                ),
                published_at=(
                    ObservedField(value=published_at, origin=origin)
                    if published_at is not None
                    else None
                ),
            )
            logger.info(
                "fetched_article_converted",
                type="observed",
                source_name=str(source_name),
            )
            return observed
        except ValueError as err:
            observed_err = err

    # Phase 4: 両方なれず → 失敗ヘルパー 1 回呼び
    _raise_conversion_failed(
        title=title,
        title_trimmed=title_trimmed,
        link=link,
        source_url=source_url,
        url_err=url_err,
        body_candidate=body_candidate,
        published_hint=published_hint,
        observed_err=observed_err,
        source_name=source_name,
    )
