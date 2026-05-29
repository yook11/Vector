"""補完を実行できる pending 行を Domain 側で構築する。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from app.audit.domain.event import EventType
from app.collection.domain.canonical_article_url import CanonicalArticleUrl
from app.collection.domain.observed_article import ObservedArticle
from app.collection.sources.article_completion_policy import ArticleCompletionPolicy
from app.collection.sources.registry import completion_policy_for
from app.collection.sources.source_name import SourceName

__all__ = [
    "ArticleCompletionPreconditionProtocol",
    "ArticleCompletionReadyBuildError",
    "ArticleCompletionReadyBuildFacts",
    "ArticleCompletionReadyBuildPendingMissingError",
    "ArticleCompletionReadyBuildPendingNotRunningError",
    "ReadyForArticleCompletion",
]


@dataclass(frozen=True, slots=True)
class ArticleCompletionReadyBuildFacts:
    """Stage 2 Ready 構築に必要な DB 射影。"""

    pending_id: int
    source_id: int
    source_name: SourceName
    status: str
    staged_attributes: dict[str, Any]
    source_url: str
    attempt_count: int


class ArticleCompletionReadyBuildError(Exception):
    """Stage 2 Ready を構築できなかった理由を表す typed error。"""

    CODE: ClassVar[str]
    EVENT_TYPE: ClassVar[EventType]
    FAILURE_KIND: ClassVar[str | None] = None
    MESSAGE: ClassVar[str]

    def __init__(self) -> None:
        super().__init__(self.MESSAGE)


class ArticleCompletionReadyBuildPendingMissingError(ArticleCompletionReadyBuildError):
    """pending_id に対応する pending 行が存在しなかった。"""

    CODE: ClassVar[str] = "completion_ready_build_blocked_pending_missing"
    EVENT_TYPE: ClassVar[EventType] = EventType.SKIPPED
    MESSAGE: ClassVar[str] = "pending row is missing for completion ready build"


class ArticleCompletionReadyBuildPendingNotRunningError(
    ArticleCompletionReadyBuildError
):
    """pending 行は存在するが completion 実行対象の running ではなかった。"""

    CODE: ClassVar[str] = "completion_ready_build_blocked_pending_not_running"
    EVENT_TYPE: ClassVar[EventType] = EventType.SKIPPED
    MESSAGE: ClassVar[str] = "pending row is not running for completion ready build"


class ArticleCompletionPreconditionProtocol(Protocol):
    """Ready 構築に必要な DB 事実だけを読む repository contract。"""

    async def load_ready_build_facts(
        self, pending_id: int
    ) -> ArticleCompletionReadyBuildFacts | None: ...


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
    ) -> ReadyForArticleCompletion:
        """DB 事実から Ready を構築し、構築不能なら typed error を投げる。"""
        facts = await repo.load_ready_build_facts(pending_id)
        if facts is None:
            raise ArticleCompletionReadyBuildPendingMissingError()

        if facts.status != "running":
            raise ArticleCompletionReadyBuildPendingNotRunningError()

        # CanonicalArticleUrlInvalidError / ObservedArticleInvalidError は
        # VO 層が reason 付きで投げる。ready は翻訳せずそのまま伝播し、where
        # (Stage.COMPLETION) は呼び出し側の監査が焼く。
        source_url = CanonicalArticleUrl.from_raw(facts.source_url)

        observed = ObservedArticle.from_staged_attributes(
            facts.staged_attributes,
            source_name=facts.source_name,
            source_url=source_url,
        )
        profile = completion_policy_for(observed.source_name)

        return cls(
            pending_id=facts.pending_id,
            source_id=facts.source_id,
            attempt_count=facts.attempt_count,
            observed=observed,
            profile=profile,
            source_url=source_url,
        )
