"""Question answering service."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.agent.contract import QuestionPlan, UnmetRequirement
from app.agent.external_search import ExternalSearchOutcome
from app.agent.internal_retrieval.article_search import InternalArticleSearchHit

__all__ = [
    "ExternalPlanSearcher",
    "InternalArticleRetriever",
    "QuestionAnsweringService",
    "RetrievalOutcome",
]


class InternalArticleRetriever(Protocol):
    async def search_plan_articles(
        self,
        plan: QuestionPlan,
    ) -> list[InternalArticleSearchHit]: ...


class ExternalPlanSearcher(Protocol):
    async def search_plan(
        self,
        plan: QuestionPlan,
        *,
        as_of: datetime,
        requested_agent_count: int | None = None,
    ) -> ExternalSearchOutcome: ...


class RetrievalOutcome(BaseModel):
    """plan 実行の純粋な結果。回答の根拠候補データと未充足要件のみを持つ。"""

    model_config = ConfigDict(frozen=True)

    internal_hits: list[InternalArticleSearchHit] = Field(default_factory=list)
    external_search: ExternalSearchOutcome | None = None
    unmet_requirements: list[UnmetRequirement] = Field(default_factory=list)


class QuestionAnsweringService:
    """Plan を受け取り、retrieval_mode ごとの検索実行に振り分ける。"""

    def __init__(
        self,
        *,
        internal_search: InternalArticleRetriever,
        external_search: ExternalPlanSearcher | None = None,
        requested_external_agent_count: int | None = None,
    ) -> None:
        self._internal_search = internal_search
        self._external_search = external_search
        self._requested_external_agent_count = requested_external_agent_count

    async def retrieve(
        self,
        plan: QuestionPlan,
        *,
        as_of: datetime,
    ) -> RetrievalOutcome:
        match plan.retrieval_mode:
            case "none":
                return RetrievalOutcome()
            case "internal":
                hits = await self._internal_search.search_plan_articles(plan)
                return RetrievalOutcome(internal_hits=hits)
            case "external":
                external = await self._search_external(plan, as_of=as_of)
                if external is not None:
                    return RetrievalOutcome(external_search=external)
                return RetrievalOutcome(
                    unmet_requirements=["external_search"],
                )
            case "internal_and_external":
                hits = await self._internal_search.search_plan_articles(plan)
                external = await self._search_external(plan, as_of=as_of)
                if external is not None:
                    return RetrievalOutcome(
                        internal_hits=hits,
                        external_search=external,
                    )
                return RetrievalOutcome(
                    internal_hits=hits,
                    unmet_requirements=["external_search"],
                )

    async def _search_external(
        self,
        plan: QuestionPlan,
        *,
        as_of: datetime,
    ) -> ExternalSearchOutcome | None:
        if self._external_search is None:
            return None
        return await self._external_search.search_plan(
            plan,
            as_of=as_of,
            requested_agent_count=self._requested_external_agent_count,
        )
