"""Question answering service."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent.contract import QuestionPlan, UnmetRequirement
from app.agent.external_search import ExternalSearchOutcome
from app.agent.internal_retrieval.article_search import InternalArticleSearchHit

__all__ = [
    "ExternalPlanSearcher",
    "InternalArticleRetriever",
    "QuestionPlanRetrievalService",
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

    @model_validator(mode="after")
    def _validate_external_search_unmet_consistency(self) -> Self:
        if (
            self.external_search is not None
            and "external_search" in self.unmet_requirements
        ):
            raise ValueError("external_search outcome cannot also be marked as unmet")
        return self


class QuestionPlanRetrievalService:
    """QuestionPlan を読んで internal/external retrieval を起動する工程。"""

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
