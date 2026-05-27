"""補完を実行できる pending 行を表す precondition 型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.collection.domain.canonical_article_url import CanonicalArticleUrl
from app.collection.domain.observed_article import ObservedArticle
from app.collection.sources.article_completion_policy import ArticleCompletionPolicy


class ArticleCompletionPreconditionProtocol(Protocol):
    """補完進行判定用の Repository contract。"""

    async def try_load_for_completion(
        self, pending_id: int
    ) -> ReadyForArticleCompletion | None: ...


@dataclass(frozen=True, slots=True)
class ReadyForArticleCompletion:
    """``status='running'`` の pending 行から作る補完入力。"""

    pending_id: int
    source_id: int
    attempt_count: int
    observed: ObservedArticle
    profile: ArticleCompletionPolicy
    source_url: CanonicalArticleUrl

    @classmethod
    async def try_advance_from(
        cls,
        *,
        pending_id: int,
        repo: ArticleCompletionPreconditionProtocol,
    ) -> ReadyForArticleCompletion | None:
        """pending_id から補完へ進める場合だけ Ready を返す。"""
        return await repo.try_load_for_completion(pending_id)
