"""``IncompleteArticle`` Entity — HTML 補完待ちの未完成記事。

Pattern H fetcher が RSS 本文を持たない場合に yield する中間 Domain 表現。
``pending_html_articles.staged_attributes`` (JSONB) に焼かれて永続化され、
Stage 2 cron poller (``dispatch_html_fetch_jobs``) で再 hydrate される。
``complete_with_html`` instance method が補完遷移の唯一の入り口 — DDD 原則
として未完成 → 完成への遷移条件は ``IncompleteArticle`` 自身の責務に集約する。

``BaseModel(frozen=True)`` は taskiq 経由ではなく不変表明のため
(memory `feedback_taskiq_basemodel_required.md` 参照)。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.article_limits import ARTICLE_TITLE_MAX_LENGTH
from app.collection.domain.completion import (
    ArticleCompletionFailed,
    ArticleCompletionFailureReason,
)
from app.collection.domain.value_objects import PublishedAt
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl


class IncompleteArticle(BaseModel):
    """Pattern H で生成され、Stage 2 で本文取得後に ``AnalyzableArticle`` へ
    昇格する不完全な記事 (本文不足) の中間 Domain 表現。

    ``prefer_html_title`` は sitemap 系ソース (RSS が title を持たない) のための
    opt-in flag。``True`` のとき HTML から抽出された title を優先する。
    """

    model_config = ConfigDict(frozen=True)

    title: str = Field(min_length=1, max_length=ARTICLE_TITLE_MAX_LENGTH)
    source_id: int = Field(gt=0)
    source_url: CanonicalArticleUrl
    published_at_hint: PublishedAt | None = None
    prefer_html_title: bool = False

    def complete_with_html(
        self,
        body: str,
        html_published_at: PublishedAt | None,
        html_title: str | None = None,
    ) -> AnalyzableArticle | ArticleCompletionFailed:
        """HTML 補完を取り込み ``AnalyzableArticle`` (passport) に昇格する。

        Merge 規則:

        - ``title``: RSS 優先 (``prefer_html_title=True`` のとき HTML 採用に切替)。
        - ``published_at``: RSS hint 優先 / HTML フォールバック / 両方欠落で
          ``ArticleCompletionFailed(code="published_at_missing")``。
        - ``body``: HTML から取得した本文をそのまま使う (RSS には存在しない)。

        ``AnalyzableArticle`` の Field invariant (length / 形式) 違反は
        ``code="ready_invariant_failed"`` で wrap して返す。
        """
        final_published = self.published_at_hint or html_published_at
        if final_published is None:
            return ArticleCompletionFailed(
                reason=ArticleCompletionFailureReason(
                    code="published_at_missing",
                    detail="rss_and_html_both_missing",
                )
            )
        final_title = (
            html_title if (self.prefer_html_title and html_title) else self.title
        )
        try:
            return AnalyzableArticle(
                title=final_title,
                body=body,
                published_at=final_published,
                source_id=self.source_id,
                source_url=self.source_url,
            )
        except ValueError as e:
            return ArticleCompletionFailed(
                reason=ArticleCompletionFailureReason(
                    code="ready_invariant_failed",
                    detail=f"invariant_violation:{e}",
                )
            )
