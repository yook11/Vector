"""``FetchedArticle`` → 「何ができたか」への総変換 (純粋関数)。

per-source 責務は body / published を信用できる形で渡せるかのみ。Ready 昇格 /
Observed 保留 / 変換不能(棄却) の最終分岐を ``convert_fetched_article`` に集約し、
想定内の 3 結末すべてに対して total にする (棄却も raise せず
``AcquisitionConversionRejection`` 値で返す)。

``AnalyzableArticle`` 不成立は想定内の正常系 (Ready 候補 ⊆ Observed 候補)。
precondition (title / URL) を満たさない entry は棄却となる。棄却理由の判定と
ログ出力は ``_reject`` に集約する。想定外 bug (precondition 通過後の invariant
違反) は本関数では catch せず素通りさせ、stream orchestrator (service) が
``unexpected_rejection`` で値化する。
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from app.collection.article_acquisition.errors import AcquisitionConversionDefect
from app.collection.article_acquisition.fetched_article import FetchedArticle
from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.article_limits import ARTICLE_TITLE_MAX_LENGTH
from app.collection.domain.canonical_article_url import (
    CanonicalArticleUrl,
    CanonicalArticleUrlInvalidError,
)
from app.collection.domain.observed_article import ObservedArticle
from app.collection.domain.value_objects import PublishedAt
from app.collection.sources.article_source import ArticleSource
from app.collection.sources.source_name import SourceName

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AcquisitionConversionRejection:
    """stream 境界で変換不能 entry を表す値。

    per-entry raise だと source stream 全体が止まるため、棄却を値に落として
    継続させる。``outcome_code`` は責任元 VO の reason を verbatim で運び (URL=
    ``SafeUrlInvalidReason`` / title 欠落・想定外=``AcquisitionConversionDefect``)、
    監査は再分類せずそれを焼くだけ。``cause`` は原因例外を保持し監査が FQN / chain
    を辿れる (URL=``CanonicalArticleUrlInvalidError`` / 想定外=本当のバグ / title
    欠落=None)。``raw_url`` は素の値で、redact は監査側の責務。
    """

    outcome_code: str
    source_name: str | None
    raw_url: str | None
    has_title: bool
    body_length: int | None
    has_published_at: bool
    cause: Exception | None


def _reject(
    *,
    outcome_code: str,
    fetched: FetchedArticle,
    source_name: SourceName,
    cause: Exception | None = None,
) -> AcquisitionConversionRejection:
    """観測スナップショット取得 + 構造化ログ + 値化の集約点。"""
    has_title = bool(fetched.title)
    body_length = len(fetched.body) if fetched.body is not None else None
    has_published_at = fetched.published_at is not None
    raw_url = fetched.url or None

    logger.info(
        "article_conversion_rejected",
        source_name=str(source_name),
        outcome_code=outcome_code,
        has_title=has_title,
        body_length=body_length,
        has_published_at=has_published_at,
    )
    return AcquisitionConversionRejection(
        outcome_code=outcome_code,
        source_name=str(source_name),
        raw_url=raw_url,
        has_title=has_title,
        body_length=body_length,
        has_published_at=has_published_at,
        cause=cause,
    )


def unexpected_rejection(
    fetched: FetchedArticle,
    *,
    source: ArticleSource,
    cause: Exception,
) -> AcquisitionConversionRejection:
    """想定外 bug を ``UNEXPECTED_ERROR`` の ``AcquisitionConversionRejection`` に
    値化する funnel。

    precondition 通過後の invariant 違反 (= ありえない筈の bug) が ``convert`` から
    漏れたとき、stream orchestrator (service) がこれを呼んで値化する。stack trace は
    ``logger.exception`` で残し、本当のバグである ``cause`` を保持して監査が FQN /
    chain を辿れるようにする (``except`` 節内から呼ばれる前提)。
    """
    logger.exception(
        "fetched_article_conversion_unexpected",
        source_name=str(source.name),
        error_class=f"{type(cause).__module__}.{type(cause).__qualname__}",
    )
    return AcquisitionConversionRejection(
        outcome_code=AcquisitionConversionDefect.UNEXPECTED_ERROR.value,
        source_name=str(source.name),
        raw_url=fetched.url or None,
        has_title=bool(fetched.title),
        body_length=len(fetched.body) if fetched.body else None,
        has_published_at=fetched.published_at is not None,
        cause=cause,
    )


def convert_fetched_article(
    fetched: FetchedArticle,
    *,
    source: ArticleSource,
    source_id: int,
) -> AnalyzableArticle | ObservedArticle | AcquisitionConversionRejection:
    """1 ``FetchedArticle`` を「何ができたか」に変換する (想定内に total)。

    URL / title は獲得型の土台 (identity)。不在 / 不正なら獲得型は成立し得ない
    ため、Phase 0 で precondition として棄却する。残りの正規化と Ready /
    Observed の構築はこの precondition 通過を前提に進む。

    Returns:
        ``AnalyzableArticle`` — body + published_at が揃い品質ゲート通過。
        ``ObservedArticle`` — title + URL は揃うが Ready 不成立 (取れた事実を
        全保存)。
        ``AcquisitionConversionRejection`` — title / URL precondition を満たさない棄却
        (raise せず値で返す)。
    """
    source_name = source.name
    origin = source.observed_origin

    title = fetched.title.strip()[:ARTICLE_TITLE_MAX_LENGTH]
    if not title:
        return _reject(
            outcome_code=AcquisitionConversionDefect.TITLE_MISSING.value,
            fetched=fetched,
            source_name=source_name,
        )

    try:
        source_url = CanonicalArticleUrl.from_raw(fetched.url)
    except CanonicalArticleUrlInvalidError as err:
        return _reject(
            outcome_code=err.reason.value,
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
