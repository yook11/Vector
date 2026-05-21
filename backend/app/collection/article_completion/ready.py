"""補完を実行してよい状態を表す precondition 型と、その構築 gateway。

補完に必要な値を全揃えで運ぶ:

- ``observed`` — 取得済み事実 (``ObservedArticle``)。
- ``profile`` — per-source 補完方針 (``ArticleCompletionPolicy``)。
- ``source_url`` — 記事 identity (``pending_html_articles.url`` 列が
  authoritative。``ObservedArticle`` は持たない)。
- ``attempt_count`` — stale worker guard / retry 予算判定の SSoT。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.collection.domain.canonical_article_url import CanonicalArticleUrl
from app.collection.domain.observed_article import ObservedArticle
from app.collection.sources.article_completion_policy import ArticleCompletionPolicy


class ArticleCompletionPreconditionProtocol(Protocol):
    """補完進行判定用の Repository contract。

    precondition (``status='running'``) を満たす場合に
    ``ReadyForArticleCompletion`` を構築して返す。
    """

    async def try_load_for_completion(
        self, pending_id: int
    ) -> ReadyForArticleCompletion | None: ...


@dataclass(frozen=True, slots=True)
class ReadyForArticleCompletion:
    """補完を実行可能な状態を表す precondition 型。

    この型が作られるのは ``status='running'`` の pending 行だけ。
    ``attempt_count`` は retry 予算判定と stale worker guard の SSoT。
    """

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
        """pending_id から補完へ進めるかを判定する gateway。

        進める条件: 同 pending_id の ``pending_html_articles`` 行が
        ``status='running'`` (cron dispatcher が claim 済)。未 claim / sweep 済 /
        close 済 / delete 済はすべて進めない。

        Returns:
            進める場合: ``ReadyForArticleCompletion``
            進めない場合: ``None`` (業務正常状態、例外ではない)
        """
        return await repo.try_load_for_completion(pending_id)
