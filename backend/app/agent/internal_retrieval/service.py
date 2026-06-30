"""Agent-facing internal search service boundary."""

from __future__ import annotations

from dataclasses import dataclass

from app.agent.contract import QuestionPlan
from app.agent.internal_retrieval.metrics import record_internal_retrieval_outcome
from app.agent.internal_retrieval.query_embedding import (
    InternalQueryEmbedder,
    InternalQueryEmbedding,
    build_internal_search_queries,
)

__all__ = ["InternalSearchService"]


@dataclass(frozen=True, slots=True)
class InternalSearchService:
    """Agent-facing internal search entrypoint for this query-embedding slice."""

    embedder: InternalQueryEmbedder

    async def embed_plan_queries(
        self,
        plan: QuestionPlan,
    ) -> list[InternalQueryEmbedding]:
        if plan.retrieval_mode not in {"internal", "internal_and_external"}:
            return []

        queries = build_internal_search_queries(plan.internal_queries)
        if not queries.queries:
            return []

        try:
            embeddings = await self.embedder.embed_queries(queries)
        except Exception:
            record_internal_retrieval_outcome(
                result="failed",
                query_count=len(queries.queries),
            )
            raise
        record_internal_retrieval_outcome(
            result="succeeded" if embeddings else "empty",
            query_count=len(queries.queries),
        )
        return embeddings
