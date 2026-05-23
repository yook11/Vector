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
    CompletionInvariantRejected,
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


CompletionFailure = FetchFailed | AcquisitionFailure | CompletionInvariantRejected
"""補完が失敗する 3 形を 1 つに揃えた閉じた値 union。

- ``FetchFailed``: origin fetch 例外を畳んだ値。
- ``AcquisitionFailure``: 取れたが使える本文でない (4 variant、証拠を保持)。
- ``CompletionInvariantRejected``: merge 後の構築不変条件違反 (例外証拠を保持)。
"""


def complete_with_html(
    observed: ObservedArticle,
    profile: ArticleCompletionPolicy,
    html: AcquiredContent,
    *,
    source_id: int,
    source_url: CanonicalArticleUrl,
) -> AnalyzableArticle | CompletionInvariantRejected:
    """抽出結果 (``AcquiredContent``) を観測値と merge し ``AnalyzableArticle`` に昇格。

    責務は薄いオーケストレーション: per-field の正本 merge は ``profile.resolve``
    (写像)、構築不変条件 (published_at 欠落含む) は ``AnalyzableArticle`` (出口契約)
    が担い、本関数は組み立てと不能の証拠化 (``CompletionInvariantRejected``) のみ。
    取得失敗 (``AcquisitionFailure``) はここに来ない (``complete`` が surface する)。
    """
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

        # 抽出結果が無ければ (AcquisitionFailure) 完成のしようがないので、取得層の
        # 判定を retry 分類付きのまま surface する。完成は抽出結果を持つ場合のみ。
        if not isinstance(html_result, AcquiredContent):
            return html_result

        return complete_with_html(
            ready.observed,
            ready.profile,
            html_result,
            source_id=ready.source_id,
            source_url=ready.source_url,
        )
