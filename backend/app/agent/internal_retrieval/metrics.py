"""Internal retrieval metrics."""

from __future__ import annotations

from typing import Literal

import logfire

InternalRetrievalResult = Literal["succeeded", "empty", "failed"]
QueryEmbeddingCacheResult = Literal["lookup_failed", "save_failed"]

_internal_retrieval_outcome_counter = logfire.metric_counter(
    "vector.agent.internal_retrieval.outcome",
    unit="1",
    description="Internal retrieval component outcome when query embedding runs",
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
) -> None:
    """Record the internal retrieval boundary outcome."""

    _internal_retrieval_outcome_counter.add(
        1,
        attributes={"result": result, "query_count": query_count},
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
