"""観測値と抽出結果をソースの補完ルールで統合し、分析できる記事を完成させる。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from app.collection.article_completion.errors import ArticleBuildRejectedError
from app.collection.domain.analyzable_article import (
    AnalyzableArticle,
    QualityTooLow,
)
from app.collection.domain.observed_article import ObservedArticle
from app.collection.domain.value_objects import PublishedAt
from app.collection.sources.article_completion_policy import (
    ArticleCompletionPolicy,
    CompletableField,
    FieldCompletionRule,
)


@dataclass(frozen=True, slots=True)
class ExtractedContent:
    """記事ページから抽出した項目を保持し、抽出できなかった項目はNoneとする。"""

    title: str | None
    body: str | None
    published_at: PublishedAt | None


def _resolve[V](
    rule: FieldCompletionRule, observed: V | None, html: V | None
) -> V | None:
    """rule に従い観測値と HTML 値を 1 フィールド分 merge する。

    - ``html_required``: HTML を正本とし観測値は無視 (HTML 欠でも fallback しない)。
    - ``html_preferred``: HTML があれば優先、なければ観測値。
    - ``observed_preferred``: 観測値があれば優先、なければ HTML。
    """
    match rule:
        case FieldCompletionRule.html_required:
            return html
        case FieldCompletionRule.html_preferred:
            return html if html else observed
        case FieldCompletionRule.observed_preferred:
            return observed if observed else html
        case _:
            assert_never(rule)


def complete_article(
    observed: ObservedArticle,
    extracted: ExtractedContent,
    completion_policy: ArticleCompletionPolicy,
    *,
    source_id: int,
) -> AnalyzableArticle:
    """観測値と抽出結果を項目ごとのルールで統合し AnalyzableArticle に昇格する。"""
    rules = completion_policy.rules
    obs_title = observed.title.value if observed.title is not None else None
    obs_body = observed.body.value if observed.body is not None else None
    obs_pub = observed.published_at.value if observed.published_at is not None else None

    built = AnalyzableArticle.build_or_reject(
        title=_resolve(rules[CompletableField.title], obs_title, extracted.title),
        body=_resolve(rules[CompletableField.body], obs_body, extracted.body),
        published_at=_resolve(
            rules[CompletableField.published_at], obs_pub, extracted.published_at
        ),
        source_id=source_id,
        source_url=observed.source_url,
    )
    if isinstance(built, QualityTooLow):
        raise ArticleBuildRejectedError(built)
    return built
