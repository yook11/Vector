"""Internal retrieval query embedding contract tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agent.evidence_collection.internal_search.query_embedding import (
    InternalQueryEmbedding,
    InternalSearchQueries,
)
from app.analysis.embedding.domain.value_objects import (
    EMBEDDING_DIMENSION,
    EmbeddingVector,
)


def _vector(value: float = 0.1) -> EmbeddingVector:
    return EmbeddingVector(root=tuple([value] * EMBEDDING_DIMENSION))


class TestInternalSearchQueries:
    def test_accepts_empty_queries_as_noop(self) -> None:
        queries = InternalSearchQueries()

        assert queries.queries == ()

    def test_accepts_at_most_three_queries(self) -> None:
        queries = InternalSearchQueries(queries=("A", "B", "C"))

        assert queries.queries == ("A", "B", "C")

    def test_strips_queries_when_constructed_directly(self) -> None:
        queries = InternalSearchQueries(queries=("  NVIDIA  ",))

        assert queries.queries == ("NVIDIA",)

    def test_rejects_more_than_three_queries_when_constructed_directly(self) -> None:
        with pytest.raises(ValidationError):
            InternalSearchQueries(queries=("A", "B", "C", "D"))

    def test_rejects_blank_query_when_constructed_directly(self) -> None:
        with pytest.raises(ValidationError):
            InternalSearchQueries(queries=("   ",))


class TestInternalQueryEmbedding:
    def test_accepts_query_and_embedding_vector(self) -> None:
        embedding = InternalQueryEmbedding(query="NVIDIA", vector=_vector())

        assert embedding.query == "NVIDIA"
        assert isinstance(embedding.vector, EmbeddingVector)

    def test_strips_query(self) -> None:
        embedding = InternalQueryEmbedding(query="  NVIDIA  ", vector=_vector())

        assert embedding.query == "NVIDIA"

    def test_rejects_blank_query(self) -> None:
        with pytest.raises(ValidationError):
            InternalQueryEmbedding(query="   ", vector=_vector())
