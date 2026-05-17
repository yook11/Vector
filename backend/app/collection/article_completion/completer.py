"""Stage 2 の補完境界 — ``ObservedArticle`` を完成形に解決する純粋関数。

``ArticleCompletionService`` が「資格判定 → 完成 → 分類 → 後始末 → 永続化」を
1 メソッドに混ぜていた問題を解くため、**完成させる**責務だけをここに切り出す。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import assert_never

from app.collection.article_completion.extractor import (
    ArticleHtmlExtractor,
    ExtractedContent,
    ExtractionEmpty,
)
from app.collection.article_completion.ready import ReadyForArticleCompletion
from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.completion import (
    ArticleCompletionFailed,
    ArticleCompletionFailureReason,
)
from app.collection.domain.observed_article import ObservedArticle
from app.collection.domain.source_completion_profile import (
    AnalyzableField,
    FieldCompletionPolicy,
    SourceCompletionProfile,
)
from app.collection.external_fetch_errors import ExternalFetchError
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl


@dataclass(frozen=True, slots=True)
class FetchFailed:
    """origin fetch が ``ExternalFetchError`` で失敗したことを表す値。

    境界で例外を値に畳むためのラッパ。元の例外は ``error`` に保持し、分類
    (``classify_external_fetch_error``) と log の ``error_class`` で使う。
    """

    error: ExternalFetchError


CompletionFailure = FetchFailed | ExtractionEmpty | ArticleCompletionFailed
"""補完が失敗する 3 形を 1 つに揃えた閉じた値 union。

- ``FetchFailed``: origin fetch 例外を畳んだ値。
- ``ExtractionEmpty``: 取れたが使える本文でない (extractor の値)。
- ``ArticleCompletionFailed``: merge / invariant 違反 (domain の値)。
"""


def _resolve[V](
    policy: FieldCompletionPolicy,
    observed_value: V | None,
    html_value: V | None,
) -> V | None:
    """policy に従い観測値と HTML 値を 1 フィールド分 merge する。

    旧規則との等価:

    - ``html_preferred`` = 旧 ``html if html else observed`` (仮タイトル特例)
    - ``observed_preferred`` = 旧 ``observed or html`` (published_at hint /
      title default。観測 title は Stage 1 invariant で常在 ⇒ 常に観測勝ち)
    - ``html_required`` = 旧 body 規則 (HTML を正本・観測無視)
    """
    match policy:
        case FieldCompletionPolicy.html_required:
            return html_value
        case FieldCompletionPolicy.html_preferred:
            return html_value if html_value else observed_value
        case FieldCompletionPolicy.observed_preferred:
            return observed_value if observed_value else html_value
        case _:
            assert_never(policy)


def complete_with_html(
    observed: ObservedArticle,
    profile: SourceCompletionProfile,
    html: ExtractedContent | ExtractionEmpty,
    *,
    source_id: int,
    source_url: CanonicalArticleUrl,
) -> AnalyzableArticle | ArticleCompletionFailed | ExtractionEmpty:
    """観測事実 + profile + HTML 抽出結果を merge し ``AnalyzableArticle`` 昇格。"""
    pol = profile.policies

    # body=html_required で抽出空 → 旧 completer 短絡と等価
    # (ExtractionEmpty を値のまま返し disposition 分類を変えない)。
    if (
        isinstance(html, ExtractionEmpty)
        and pol[AnalyzableField.body] is FieldCompletionPolicy.html_required
    ):
        return html

    html_title = html.title if isinstance(html, ExtractedContent) else None
    html_body = html.body if isinstance(html, ExtractedContent) else None
    html_pub = html.published_at if isinstance(html, ExtractedContent) else None

    obs_title = observed.title.value if observed.title is not None else None
    obs_body = observed.body.value if observed.body is not None else None
    obs_pub = observed.published_at.value if observed.published_at is not None else None

    final_title = _resolve(pol[AnalyzableField.title], obs_title, html_title)
    final_body = _resolve(pol[AnalyzableField.body], obs_body, html_body)
    final_published = _resolve(pol[AnalyzableField.published_at], obs_pub, html_pub)

    if final_published is None:
        return ArticleCompletionFailed(
            reason=ArticleCompletionFailureReason(
                code="published_at_missing",
                detail="rss_and_html_both_missing",
            )
        )
    try:
        return AnalyzableArticle(
            title=final_title,
            body=final_body,
            published_at=final_published,
            source_id=source_id,
            source_url=source_url,
        )
    except ValueError as e:
        return ArticleCompletionFailed(
            reason=ArticleCompletionFailureReason(
                code="ready_invariant_failed",
                detail=f"invariant_violation:{e}",
            )
        )


class ArticleHtmlCompleter:
    """HTMLから抽出をして AnalyzableArticle を完成させる責任を持つ。"""

    def __init__(
        self,
        extractor_factory: Callable[[], ArticleHtmlExtractor] = ArticleHtmlExtractor,
    ) -> None:
        self._extractor_factory = extractor_factory

    async def complete(
        self, ready: ReadyForArticleCompletion
    ) -> AnalyzableArticle | CompletionFailure:

        extractor = self._extractor_factory()

        try:
            html_result = await extractor.fetch(ready.source_url.as_safe_url())
        except ExternalFetchError as exc:
            return FetchFailed(error=exc)

        return complete_with_html(
            ready.observed,
            ready.profile,
            html_result,
            source_id=ready.source_id,
            source_url=ready.source_url,
        )
