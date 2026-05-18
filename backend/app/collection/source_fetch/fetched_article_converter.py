"""``FetchedArticle`` → ``AnalyzableArticle | ObservedArticle`` 変換 (純粋関数)。

per-source 責務は body / published を信用できる形で渡せるかのみ。Ready 昇格 /
Observed 保留 / 変換不能 の最終分岐を本変換器に集約する。公開 API は
``convert_fetched_article`` のみ。変換不能 entry は ``None`` を返さず
``FetchedArticleConversionError`` を raise する (stream を止めないための値化は
``ArticleFetcher`` の責務)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.article_limits import (
    ARTICLE_BODY_MAX_LENGTH,
    ARTICLE_BODY_MIN_LENGTH,
    ARTICLE_TITLE_MAX_LENGTH,
)
from app.collection.domain.observed_article import (
    ObservedArticle,
    ObservedField,
    ObservedOrigin,
)
from app.collection.domain.value_objects import PublishedAt
from app.collection.source_fetch.errors import (
    ConversionReason,
    FetchedArticleConversionError,
)
from app.collection.source_fetch.fetched_article import FetchedArticle
from app.collection.sources.article_source import ArticleSource
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl
from app.shared.value_objects.source_name import SourceName


@dataclass(frozen=True, slots=True)
class ConversionRejection:
    """stream 境界で変換不能 entry を表す値。

    per-entry raise だと source stream 全体が止まるため、例外を値に落として
    継続させる。原因例外を内包し監査が ``__cause__`` 連鎖を辿れる。
    """

    error: FetchedArticleConversionError


def _ready_failure_reason(
    *,
    ready_precluded: bool,
    body_candidate: str | None,
    published_at: PublishedAt | None,
) -> ConversionReason:
    """Ready 構築が成立しなかった理由を 1 つに確定する (上から最初の不成立)。"""
    if ready_precluded:
        return ConversionReason.READY_PRECLUDED
    if body_candidate is None:
        return ConversionReason.BODY_ABSENT
    if len(body_candidate) < ARTICLE_BODY_MIN_LENGTH:
        return ConversionReason.BODY_TOO_SHORT
    if len(body_candidate) > ARTICLE_BODY_MAX_LENGTH:
        return ConversionReason.BODY_TOO_LONG
    if published_at is None:
        return ConversionReason.PUBLISHED_ABSENT
    return ConversionReason.ANALYZABLE_INVARIANT


def _convert_fetched_article(
    *,
    title: str | None,
    link: str | None,
    body_candidate: str | None,
    published_hint: datetime | None,
    source_id: int,
    source_name: SourceName,
    origin: ObservedOrigin,
    ready_precluded: bool = False,
) -> AnalyzableArticle | ObservedArticle:
    """変換の共通実装 (private)。獲得型の直接構築をこの関数内に閉じ込める。

    どちらにも変換できない entry は ``FetchedArticleConversionError`` を raise。
    """
    raw_url = link
    has_title = title is not None
    body_length = len(body_candidate) if body_candidate is not None else None
    has_published_at = published_hint is not None

    def _fail(
        analyzable_reason: ConversionReason,
        observed_reason: ConversionReason,
    ) -> FetchedArticleConversionError:
        return FetchedArticleConversionError(
            f"analyzable rejected: {analyzable_reason}; "
            f"observed rejected: {observed_reason}",
            analyzable_reason=analyzable_reason,
            observed_reason=observed_reason,
            source_name=str(source_name),
            raw_url=raw_url,
            has_title=has_title,
            body_length=body_length,
            has_published_at=has_published_at,
        )

    if title is None:
        raise _fail(
            ConversionReason.MISSING_TITLE, ConversionReason.MISSING_TITLE
        ) from None
    title_trimmed = title.strip()[:ARTICLE_TITLE_MAX_LENGTH]
    if not title_trimmed:
        raise _fail(
            ConversionReason.MISSING_TITLE, ConversionReason.MISSING_TITLE
        ) from None

    if not link:
        raise _fail(
            ConversionReason.MISSING_URL, ConversionReason.MISSING_URL
        ) from None
    try:
        source_url = CanonicalArticleUrl(link)
    except ValueError as err:
        raise _fail(ConversionReason.INVALID_URL, ConversionReason.INVALID_URL) from err

    # tz-naive datetime は published として採用しない (PublishedAt が拒否)。
    # 採用できなかった場合は Observed に published_at=None で流す。
    published_at: PublishedAt | None = None
    if published_hint is not None:
        try:
            published_at = PublishedAt(value=published_hint)
        except ValueError:
            published_at = None

    can_build_ready = (
        not ready_precluded
        and body_candidate is not None
        and ARTICLE_BODY_MIN_LENGTH <= len(body_candidate) <= ARTICLE_BODY_MAX_LENGTH
        and published_at is not None
    )
    if can_build_ready:
        try:
            return AnalyzableArticle(
                title=title_trimmed,
                body=body_candidate,
                published_at=published_at,
                source_id=source_id,
                source_url=source_url,
            )
        except ValueError:
            # Ready 二次的制約違反 (title sanitize 等の domain 側 invariant) は
            # Observed fallback で救う。変換不能には落とさない (recovery 性優先)。
            pass

    # 取れた事実は全部保存する (要否は profile が決める)。body は現状 merge で
    # 無視されるが観測事実としては保持する。
    try:
        return ObservedArticle(
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
    except ValueError as err:
        raise _fail(
            _ready_failure_reason(
                ready_precluded=ready_precluded,
                body_candidate=body_candidate,
                published_at=published_at,
            ),
            ConversionReason.OBSERVED_BUILD_FAILED,
        ) from err


def convert_fetched_article(
    fetched: FetchedArticle,
    *,
    source: ArticleSource,
    source_id: int,
) -> AnalyzableArticle | ObservedArticle:
    """1 ``FetchedArticle`` を獲得型に変換する。

    空 str / ``None`` の "不在" 表現を正規化してから判定する。

    Returns:
        ``AnalyzableArticle`` — body + published_at が揃い品質ゲート通過。
        ``ObservedArticle`` — title + URL は揃うが Ready 不成立 (取れた事実を
        全保存)。

    Raises:
        FetchedArticleConversionError: title / URL 無効、または Observed 構築も
            失敗した entry。
    """
    return _convert_fetched_article(
        title=fetched.title or None,
        link=fetched.url or None,
        body_candidate=fetched.body,
        published_hint=fetched.published_at,
        source_id=source_id,
        source_name=source.name,
        origin=source.observed_origin,
        ready_precluded=source.completion_profile.precludes_stage1_ready(),
    )
