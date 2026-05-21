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
