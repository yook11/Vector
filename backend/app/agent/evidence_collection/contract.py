"""Evidence collection contract: engine ports and outcome DTO."""

from __future__ import annotations

from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent.contract import EvidenceCollectionFailure
from app.agent.evidence_collection.external_search import ExternalSearchOutcome
from app.agent.evidence_collection.internal_search.article_search import (
    InternalArticleSearchHit,
)
from app.agent.evidence_collection.internal_search.query_embedding import (
    InternalSearchQueries,
)

__all__ = [
    "EvidenceCollectionOutcome",
    "InternalArticleRetriever",
]


class InternalArticleRetriever(Protocol):
    async def search_articles(
        self,
        queries: InternalSearchQueries,
    ) -> list[InternalArticleSearchHit]: ...


class EvidenceCollectionOutcome(BaseModel):
    """plan 実行の純粋な結果。根拠候補データと失敗した収集経路を持つ。"""

    model_config = ConfigDict(frozen=True)

    internal_hits: list[InternalArticleSearchHit] = Field(default_factory=list)
    external_search: ExternalSearchOutcome | None = None
    collection_failures: list[EvidenceCollectionFailure] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_collection_failure_consistency(self) -> Self:
        expected_order = {"internal_search": 0, "external_search": 1}
        if len(self.collection_failures) != len(set(self.collection_failures)):
            raise ValueError("collection failures must be unique")
        if self.collection_failures != sorted(
            self.collection_failures,
            key=expected_order.__getitem__,
        ):
            raise ValueError("collection failures must use canonical order")
        if "internal_search" in self.collection_failures and self.internal_hits:
            raise ValueError(
                "internal hits cannot coexist with internal search failure"
            )
        if (
            self.external_search is not None
            and "external_search" in self.collection_failures
        ):
            raise ValueError("external_search outcome cannot also be marked as failed")
        return self
