"""Internal retrieval metrics."""

from __future__ import annotations

from typing import Literal

import logfire

InternalRetrievalResult = Literal["succeeded", "empty", "failed"]
InternalRetrievalFailurePhase = Literal[
    "query_embedding",
    "article_search",
    "unknown",
]
QueryEmbeddingCacheResult = Literal["lookup_failed", "save_failed"]

_internal_retrieval_outcome_counter = logfire.metric_counter(
    "vector.agent.internal_retrieval.outcome",
    unit="1",
    description="Internal search outcome including embedding and article lookup",
)

_query_embedding_cache_counter = logfire.metric_counter(
    "vector.agent.internal_retrieval.query_embedding_cache",
    unit="1",
    description="Best-effort query embedding cache failures",
)


def record_internal_retrieval_outcome(
    *,
    result: InternalRetrievalResult,
    query_count: int,
    failure_phase: InternalRetrievalFailurePhase | None = None,
) -> None:
    """Record the internal retrieval boundary outcome."""

    attributes: dict[str, str | int] = {
        "result": result,
        "query_count": query_count,
    }
    if failure_phase is not None:
        attributes["failure_phase"] = failure_phase
    _internal_retrieval_outcome_counter.add(
        1,
        attributes=attributes,
    )


def record_query_embedding_cache_outcome(
    *,
    result: QueryEmbeddingCacheResult,
    query_count: int,
) -> None:
    """Record best-effort query embedding cache failures."""

    _query_embedding_cache_counter.add(
        1,
        attributes={"result": result, "query_count": query_count},
    )
