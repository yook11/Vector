"""記事完成ポリシー — Source が持つ、フィールド単位の補完正本ルール。

どのフィールドを HTML 補完で正本とするかはソースの出自で決まる
(RSS は body を欠き、sitemap 系は title を欠く)。ポリシーを
``ArticleCompletionPolicy`` に集約し ``ArticleSource`` 集約が所有する。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import assert_never

from app.collection.domain.value_objects import PublishedAt


class CompletableField(StrEnum):
    """``AnalyzableArticle`` のうち補完対象となるフィールド。

    ``source_id`` / ``source_url`` は identity であり常在するため対象外。
    """

    title = "title"
    body = "body"
    published_at = "published_at"


class FieldCompletionRule(StrEnum):
    """フィールド単位の補完正本ルール。

    - ``html_required``: 観測値が無い / 妥当でないときは HTML 補完が必須。
      Stage-1 で妥当な値が取れていれば Ready 条件を満たせる。
    - ``html_preferred``: 観測値があっても HTML を正本 (sitemap / listing 系)。
    - ``observed_preferred``: 観測値が勝ち、HTML は fallback。
    """

    html_required = "html_required"
    html_preferred = "html_preferred"
    observed_preferred = "observed_preferred"


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


@dataclass(frozen=True, slots=True)
class ResolvedFields:
    """policy が観測値と HTML 値を merge した後の各 field 確定値 (構築前材料)。

    construct はしない。``None`` でも値のまま返し、構築可否は
    ``AnalyzableArticle`` が判定する (本型は写像の出力であって不変条件ではない)。
    """

    title: str | None
    body: str | None
    published_at: PublishedAt | None


@dataclass(frozen=True, slots=True)
class ArticleCompletionPolicy:
    """全 ``CompletableField`` → ``FieldCompletionRule`` の全域写像。

    ``rules`` は ``__post_init__`` で全域性を検証し、``MappingProxyType``
    でコピー固定する (frozen dataclass でも内包 dict は可変なため)。
    """

    rules: Mapping[CompletableField, FieldCompletionRule]

    def __post_init__(self) -> None:
        missing = set(CompletableField) - set(self.rules)
        if missing:
            msg = f"policy missing rule for {sorted(f.value for f in missing)}"
            raise ValueError(msg)
        object.__setattr__(self, "rules", MappingProxyType(dict(self.rules)))

    def requires_html_completion(self) -> bool:
        """policy が HTML 補完を必要とするか (= ``html_preferred`` field を持つか)。

        ``html_preferred`` の field は正本が Stage-2 HTML でしか確定しない。
        1 つでもあれば、観測事実だけで品質ゲートを満たしても Stage-1 Ready
        昇格させず ObservedArticle 保留に落とし、HTML 補完で正本上書きの機会
        を残す。
        """
        return any(p is FieldCompletionRule.html_preferred for p in self.rules.values())

    def resolve(
        self,
        *,
        observed_title: str | None,
        html_title: str | None,
        observed_body: str | None,
        html_body: str | None,
        observed_published_at: PublishedAt | None,
        html_published_at: PublishedAt | None,
    ) -> ResolvedFields:
        """観測値と HTML 値を per-field rule で merge し ``ResolvedFields`` を返す。

        「どの源を各 field の正本にするか」の写像であり、construct はしない。
        ``published_at`` が両源 ``None`` でも ``None`` を載せて返すだけで、完成可否
        (失敗証拠化) は呼び出し側 (completer) の責務。受けるのは primitive 値で、
        ``AcquiredContent`` / ``ObservedArticle`` 型に依存しない (import 循環回避)。
        """
        return ResolvedFields(
            title=_resolve(
                self.rules[CompletableField.title], observed_title, html_title
            ),
            body=_resolve(self.rules[CompletableField.body], observed_body, html_body),
            published_at=_resolve(
                self.rules[CompletableField.published_at],
                observed_published_at,
                html_published_at,
            ),
        )


# 大多数のソース: title/published_at は観測 (RSS) を正本に、body のみ HTML 必須。
DEFAULT_POLICY = ArticleCompletionPolicy(
    {
        CompletableField.title: FieldCompletionRule.observed_preferred,
        CompletableField.body: FieldCompletionRule.html_required,
        CompletableField.published_at: FieldCompletionRule.observed_preferred,
    }
)

# sitemap / listing 系: RSS が真の title を持たず HTML 側が正本。
HTML_TITLE_POLICY = ArticleCompletionPolicy(
    {
        CompletableField.title: FieldCompletionRule.html_preferred,
        CompletableField.body: FieldCompletionRule.html_required,
        CompletableField.published_at: FieldCompletionRule.observed_preferred,
    }
)
