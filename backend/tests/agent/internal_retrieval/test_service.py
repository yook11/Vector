"""Internal search service query embedding tests."""

from __future__ import annotations

import json
from typing import Any

import pytest
from logfire.testing import CaptureLogfire

from app.agent.contract import QuestionPlan, RetrievalMode
from app.agent.internal_retrieval.query_embedding import (
    InternalQueryEmbedding,
    InternalSearchQueries,
)
from app.agent.internal_retrieval.service import InternalSearchService
from app.analysis.embedding.domain.value_objects import (
    EMBEDDING_DIMENSION,
    EmbeddingVector,
)
from tests.logfire._metric_helpers import collected_metrics, sum_counter_for_result

_METRIC = "vector.agent.internal_retrieval.outcome"


def _vector(value: float = 0.1) -> EmbeddingVector:
    return EmbeddingVector(root=tuple([value] * EMBEDDING_DIMENSION))


def _plan(
    mode: RetrievalMode,
    *,
    internal_queries: list[str] | None = None,
    external_queries: list[str] | None = None,
) -> QuestionPlan:
    if mode == "internal" and internal_queries is None:
        internal_queries = ["internal query"]
    if mode == "external" and external_queries is None:
        external_queries = ["external query"]
    if mode == "internal_and_external":
        internal_queries = internal_queries or ["internal query"]
        external_queries = external_queries or ["external query"]
    return QuestionPlan(
        retrieval_mode=mode,
        internal_queries=internal_queries or [],
        external_queries=external_queries or [],
        reason="test reason",
    )


class FakeInternalQueryEmbedder:
    def __init__(
        self,
        *,
        empty_result: bool = False,
        error: Exception | None = None,
    ) -> None:
        self.calls: list[InternalSearchQueries] = []
        self.empty_result = empty_result
        self.error = error

    async def embed_queries(
        self,
        queries: InternalSearchQueries,
    ) -> list[InternalQueryEmbedding]:
        self.calls.append(queries)
        if self.error is not None:
            raise self.error
        if self.empty_result:
            return []
        return [
            InternalQueryEmbedding(query=query, vector=_vector())
            for query in queries.queries
        ]


def _metric_attributes(
    metrics: list[dict[str, Any]],
    metric_name: str,
) -> list[dict[str, Any]]:
    metric = next((item for item in metrics if item["name"] == metric_name), None)
    if metric is None:
        return []
    return [
        data_point.get("attributes", {}) for data_point in metric["data"]["data_points"]
    ]


class TestInternalSearchService:
    async def test_internal_plan_embeds_normalized_queries(
        self,
        capfire: CaptureLogfire,
    ) -> None:
        embedder = FakeInternalQueryEmbedder()
        service = InternalSearchService(embedder=embedder)

        embeddings = await service.embed_plan_queries(
            _plan(
                "internal",
                internal_queries=["  NVIDIA  ", "nvidia", "OpenAI", "Apple", "Google"],
            )
        )

        assert [embedding.query for embedding in embeddings] == [
            "NVIDIA",
            "OpenAI",
            "Apple",
        ]
        assert [call.queries for call in embedder.calls] == [
            ("NVIDIA", "OpenAI", "Apple")
        ]
        metrics = collected_metrics(capfire)
        assert sum_counter_for_result(metrics, _METRIC, "succeeded") == 1
        assert _metric_attributes(metrics, _METRIC) == [
            {"result": "succeeded", "query_count": 3}
        ]

    async def test_internal_and_external_plan_embeds_internal_queries(self) -> None:
        embedder = FakeInternalQueryEmbedder()
        service = InternalSearchService(embedder=embedder)

        embeddings = await service.embed_plan_queries(
            _plan("internal_and_external", internal_queries=["NVIDIA"])
        )

        assert [embedding.query for embedding in embeddings] == ["NVIDIA"]
        assert [call.queries for call in embedder.calls] == [("NVIDIA",)]

    async def test_none_plan_skips_query_embedding(self) -> None:
        embedder = FakeInternalQueryEmbedder()
        service = InternalSearchService(embedder=embedder)

        embeddings = await service.embed_plan_queries(_plan("none"))

        assert embeddings == []
        assert embedder.calls == []

    async def test_external_plan_skips_query_embedding(self) -> None:
        embedder = FakeInternalQueryEmbedder()
        service = InternalSearchService(embedder=embedder)

        embeddings = await service.embed_plan_queries(_plan("external"))

        assert embeddings == []
        assert embedder.calls == []

    async def test_empty_embedder_result_records_empty_metric(
        self,
        capfire: CaptureLogfire,
    ) -> None:
        embedder = FakeInternalQueryEmbedder(empty_result=True)
        service = InternalSearchService(embedder=embedder)

        embeddings = await service.embed_plan_queries(
            _plan("internal", internal_queries=["NVIDIA"])
        )

        assert embeddings == []
        metrics = collected_metrics(capfire)
        assert sum_counter_for_result(metrics, _METRIC, "empty") == 1
        assert _metric_attributes(metrics, _METRIC) == [
            {"result": "empty", "query_count": 1}
        ]

    async def test_embedder_failure_records_failed_metric(
        self,
        capfire: CaptureLogfire,
    ) -> None:
        embedder = FakeInternalQueryEmbedder(error=RuntimeError("embedder down"))
        service = InternalSearchService(embedder=embedder)

        with pytest.raises(RuntimeError, match="embedder down"):
            await service.embed_plan_queries(
                _plan("internal", internal_queries=["NVIDIA secret query"])
            )

        metrics = collected_metrics(capfire)
        assert sum_counter_for_result(metrics, _METRIC, "failed") == 1
        assert _metric_attributes(metrics, _METRIC) == [
            {"result": "failed", "query_count": 1}
        ]
        dumped = json.dumps(metrics, default=str, ensure_ascii=False)
        assert "NVIDIA secret query" not in dumped
