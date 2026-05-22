"""``ObservedArticle`` を完成形に解決する純粋境界 (副作用なし)。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.collection.article_completion.acquirer import (
    AcquiredContent,
    ArticleHtmlAcquirer,
)
from app.collection.article_completion.acquisition_failure import AcquisitionFailure
from app.collection.article_completion.completion_failure import (
    ArticleCompletionFailure,
    CompletionInvariantRejected,
    PublishedAtMissing,
)
from app.collection.article_completion.ready import ReadyForArticleCompletion
from app.collection.domain.analyzable_article import (
    AnalyzableArticle,
    QualityTooLow,
)
from app.collection.domain.canonical_article_url import CanonicalArticleUrl
from app.collection.domain.observed_article import ObservedArticle
from app.collection.external_fetch_errors import ExternalFetchError
from app.collection.sources.article_completion_policy import ArticleCompletionPolicy


@dataclass(frozen=True, slots=True)
class FetchFailed:
    """origin fetch が ``ExternalFetchError`` で失敗したことを表す値。

    元の例外は ``error`` に保持し、失敗分類と log で使う。
    """

    error: ExternalFetchError


CompletionFailure = FetchFailed | AcquisitionFailure | ArticleCompletionFailure
"""補完が失敗する 3 形を 1 つに揃えた閉じた値 union。

- ``FetchFailed``: origin fetch 例外を畳んだ値。
- ``AcquisitionFailure``: 取れたが使える本文でない (4 variant、証拠を保持)。
- ``ArticleCompletionFailure``: merge / invariant 違反 (2 variant、証拠を保持)。
"""


def complete_with_html(
    observed: ObservedArticle,
    profile: ArticleCompletionPolicy,
    html: AcquiredContent | AcquisitionFailure,
    *,
    source_id: int,
    source_url: CanonicalArticleUrl,
) -> AnalyzableArticle | ArticleCompletionFailure | AcquisitionFailure:
    """観測事実 + profile + HTML 取得結果を merge し ``AnalyzableArticle`` 昇格。

    責務は薄いオーケストレーション: per-field の正本 merge は ``profile.resolve``
    (写像)、構築不変条件は ``AnalyzableArticle`` (出口契約) が担い、本関数は
    precondition gate と失敗の証拠化 (``PublishedAtMissing`` /
    ``CompletionInvariantRejected``) だけを行う。
    """
    # precondition gate: body=html_required で HTML 取得が失敗していれば、resolve
    # させる前に AcquisitionFailure を値のまま返す (retry 分類を後段へ流す)。
    if not isinstance(html, AcquiredContent) and profile.body_requires_html():
        return html

    html_title = html.title if isinstance(html, AcquiredContent) else None
    html_body = html.body if isinstance(html, AcquiredContent) else None
    html_pub = html.published_at if isinstance(html, AcquiredContent) else None

    obs_title = observed.title.value if observed.title is not None else None
    obs_body = observed.body.value if observed.body is not None else None
    obs_pub = observed.published_at.value if observed.published_at is not None else None

    resolved = profile.resolve(
        observed_title=obs_title,
        html_title=html_title,
        observed_body=obs_body,
        html_body=html_body,
        observed_published_at=obs_pub,
        html_published_at=html_pub,
    )

    if resolved.published_at is None:
        return PublishedAtMissing(
            observed_had_value=obs_pub is not None,
            html_had_value=html_pub is not None,
        )
    built = AnalyzableArticle.build_or_reject(
        title=resolved.title,
        body=resolved.body,
        published_at=resolved.published_at,
        source_id=source_id,
        source_url=source_url,
    )
    if isinstance(built, QualityTooLow):
        return CompletionInvariantRejected(
            error_class=built.error_class,
            error_message=built.error_message,
        )
    return built


class ArticleHtmlCompleter:
    """HTMLから抽出をして AnalyzableArticle を完成させる責任を持つ。"""

    def __init__(
        self,
        acquirer_factory: Callable[[], ArticleHtmlAcquirer] = ArticleHtmlAcquirer,
    ) -> None:
        self._acquirer_factory = acquirer_factory

    async def complete(
        self, ready: ReadyForArticleCompletion
    ) -> AnalyzableArticle | CompletionFailure:

        acquirer = self._acquirer_factory()

        try:
            html_result = await acquirer.acquire(ready.source_url.as_safe_url())
        except ExternalFetchError as exc:
            return FetchFailed(error=exc)

        return complete_with_html(
            ready.observed,
            ready.profile,
            html_result,
            source_id=ready.source_id,
            source_url=ready.source_url,
        )
