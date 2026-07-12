"""Evidence collection contract: engine ports and outcome DTO."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent.contract import UnmetRequirement
from app.agent.evidence_collection.external_search import ExternalSearchOutcome
from app.agent.evidence_collection.internal_search.article_search import (
    InternalArticleSearchHit,
)
from app.agent.evidence_collection.internal_search.query_embedding import (
    InternalSearchQueries,
)
from app.agent.planning.contract import ExternalResearchTask

__all__ = [
    "EvidenceCollectionOutcome",
    "ExternalPlanSearcher",
    "InternalArticleRetriever",
]


class InternalArticleRetriever(Protocol):
    async def search_articles(
        self,
        queries: InternalSearchQueries,
    ) -> list[InternalArticleSearchHit]: ...


class ExternalPlanSearcher(Protocol):
    async def search(
        self,
        external_research_tasks: list[ExternalResearchTask],
        *,
        target_time_window: str | None,
        as_of: datetime,
        requested_agent_count: int | None = None,
    ) -> ExternalSearchOutcome: ...


class EvidenceCollectionOutcome(BaseModel):
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
