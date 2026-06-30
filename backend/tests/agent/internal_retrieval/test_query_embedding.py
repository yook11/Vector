"""Internal retrieval query embedding contract tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agent.internal_retrieval.query_embedding import (
    MAX_INTERNAL_QUERIES,
    InternalQueryEmbedding,
    InternalSearchQueries,
    build_internal_search_queries,
)
from app.analysis.embedding.domain.value_objects import (
    EMBEDDING_DIMENSION,
    EmbeddingVector,
)


def _vector(value: float = 0.1) -> EmbeddingVector:
    return EmbeddingVector(root=tuple([value] * EMBEDDING_DIMENSION))


class TestBuildInternalSearchQueries:
    def test_strips_queries(self) -> None:
        queries = build_internal_search_queries(["  NVIDIA earnings  "])

        assert queries.queries == ("NVIDIA earnings",)

    def test_drops_blank_queries(self) -> None:
        queries = build_internal_search_queries(["NVIDIA", "   ", "OpenAI"])

        assert queries.queries == ("NVIDIA", "OpenAI")

    def test_deduplicates_with_casefold_key(self) -> None:
        queries = build_internal_search_queries(["NVIDIA", "nvidia", "OpenAI"])

        assert queries.queries == ("NVIDIA", "OpenAI")

    def test_caps_to_three_queries_without_raising(self) -> None:
        queries = build_internal_search_queries(["A", "B", "C", "D"])

        assert len(queries.queries) == MAX_INTERNAL_QUERIES
        assert queries.queries == ("A", "B", "C")

    def test_preserves_input_order(self) -> None:
        queries = build_internal_search_queries(["B", "A", "C"])

        assert queries.queries == ("B", "A", "C")


class TestInternalSearchQueries:
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
