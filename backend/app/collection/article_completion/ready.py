"""補完を実行できる incomplete article 行を Domain 側で構築する。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from app.collection.domain.canonical_article_url import CanonicalArticleUrl
from app.collection.domain.observed_article import ObservedArticle
from app.collection.sources.article_completion_policy import ArticleCompletionPolicy
from app.collection.sources.registry import completion_policy_for
from app.collection.sources.source_name import SourceName

__all__ = [
    "ArticleCompletionPreconditionProtocol",
    "ArticleCompletionReadyBuildError",
    "ArticleCompletionReadyBuildFacts",
    "ArticleCompletionReadyBuildIncompleteArticleMissingError",
    "ArticleCompletionReadyBuildIncompleteArticleNotRunningError",
    "ReadyForArticleCompletion",
]


@dataclass(frozen=True, slots=True)
class ArticleCompletionReadyBuildFacts:
    """Stage 2 Ready 構築に必要な DB 射影。"""

    incomplete_article_id: int
    source_id: int
    source_name: SourceName
    status: str
    observed_article: dict[str, Any]
    source_url: str
    attempt_count: int


class ArticleCompletionReadyBuildError(Exception):
    """Stage 2 Ready を構築できなかった benign な skip を表す typed error。

    対象消滅 / 別 worker 完了済み等の冪等 skip で、task 側は log のみに逃がし
    監査には焼かない (audit skip 逃がしポリシー)。VO 構築失敗はこの型を継承せず
    別途伝播し、呼び出し側の監査が failed として焼く。
    """

    CODE: ClassVar[str]
    MESSAGE: ClassVar[str]

    def __init__(self) -> None:
        super().__init__(self.MESSAGE)


class ArticleCompletionReadyBuildIncompleteArticleMissingError(
    ArticleCompletionReadyBuildError
):
    """incomplete_article_id に対応する incomplete article 行が存在しなかった。"""

    CODE: ClassVar[str] = "completion_ready_build_blocked_incomplete_article_missing"
    MESSAGE: ClassVar[str] = (
        "incomplete article row is missing for completion ready build"
    )


class ArticleCompletionReadyBuildIncompleteArticleNotRunningError(
    ArticleCompletionReadyBuildError
):
    """incomplete article 行は存在するが completion 実行対象の running ではなかった。"""

    CODE: ClassVar[str] = (
        "completion_ready_build_blocked_incomplete_article_not_running"
    )
    MESSAGE: ClassVar[str] = (
        "incomplete article row is not running for completion ready build"
    )


class ArticleCompletionPreconditionProtocol(Protocol):
    """Ready 構築に必要な DB 事実だけを読む repository contract。"""

    async def load_ready_build_facts(
        self, incomplete_article_id: int
    ) -> ArticleCompletionReadyBuildFacts | None: ...


@dataclass(frozen=True, slots=True)
class ReadyForArticleCompletion:
    """``status='running'`` の incomplete article 行から作る補完入力。"""

    incomplete_article_id: int
    source_id: int
    attempt_count: int
    observed: ObservedArticle
    completion_policy: ArticleCompletionPolicy
    source_url: CanonicalArticleUrl

    @classmethod
    async def try_advance_from(
        cls,
        *,
        incomplete_article_id: int,
        repo: ArticleCompletionPreconditionProtocol,
    ) -> ReadyForArticleCompletion:
        """DB 事実から Ready を構築し、構築不能なら typed error を投げる。"""
        facts = await repo.load_ready_build_facts(incomplete_article_id)
        if facts is None:
            raise ArticleCompletionReadyBuildIncompleteArticleMissingError()

        if facts.status != "running":
            raise ArticleCompletionReadyBuildIncompleteArticleNotRunningError()

        # CanonicalArticleUrlInvalidError / ObservedArticleInvalidError は
        # VO 層が reason 付きで投げる。ready は翻訳せずそのまま伝播し、where
        # (Stage.COMPLETION) は呼び出し側の監査が焼く。
        source_url = CanonicalArticleUrl.from_raw(facts.source_url)

        observed = ObservedArticle.try_build(
            observed_article=facts.observed_article,
            source_name=facts.source_name,
            source_url=source_url,
        )
        completion_policy = completion_policy_for(observed.source_name)

        return cls(
            incomplete_article_id=facts.incomplete_article_id,
            source_id=facts.source_id,
            attempt_count=facts.attempt_count,
            observed=observed,
            completion_policy=completion_policy,
            source_url=source_url,
        )
