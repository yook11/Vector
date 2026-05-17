"""Profile 駆動の補完昇格 — ``ObservedArticle`` を ``AnalyzableArticle`` へ。

記事は補完ポリシーを持てない (policy は per-source) ため、未完成 → 完成の
遷移を instance method ではなく free function に切り出し
``SourceCompletionProfile`` 駆動で merge する。``AnalyzableArticle`` を作る
唯一の経路 = smart constructor (parse, don't validate)。

挙動は現 2 ポリシー組合せ (default / anthropic·ornl) と **完全等価**
(spec §7 回帰不変。下表):

| field | 旧 (default) | 旧 (anthropic/ornl) | 新 policy |
|---|---|---|---|
| title | 常に観測 | ``html or 観測`` | observed_preferred / html_preferred ⇒ 同一 |
| body | 常に HTML | 常に HTML | html_required ⇒ 同一 |
| published_at | ``hint or html`` | 同左 | observed_preferred ⇒ 同一 |

副作用なし。失敗 code/detail は ``domain/completion.py`` を文字列まで流用
(``disposition.py`` / テストがキーにする)。``ExtractionEmpty`` は値のまま
返し下流の分類を一切変えない (現 ``completer.py:86`` 短絡と等価)。
"""

from __future__ import annotations

from typing import assert_never

from app.collection.article_completion.extractor import (
    ExtractedContent,
    ExtractionEmpty,
)
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
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl


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
    """観測事実 + profile + HTML 抽出結果を merge し ``AnalyzableArticle`` 昇格。

    ``source_id`` / ``source_url`` は identity であり ``ObservedArticle`` は
    持たない (pending 行の関心) ため呼び出し側 (``ReadyForArticleCompletion``)
    が渡す。
    """
    pol = profile.policies

    # body=html_required で抽出空 → 現 completer.py:86 の短絡と等価
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
