"""Internal search service query embedding tests."""

from __future__ import annotations

import json
from typing import Any

import pytest
from logfire.testing import CaptureLogfire

from app.agent.internal_retrieval.article_search import (
    InternalArticleContent,
    InternalArticleSearchHit,
)
from app.agent.internal_retrieval.query_embedding import (
    InternalQueryEmbedding,
    InternalSearchQueries,
)
from app.agent.internal_retrieval.service import InternalSearchService
from app.analysis.analyzed_article import InScopeAnalyzedArticle
from app.analysis.assessment.domain.result import InScope, InScopeCategory
from app.analysis.embedding.domain.value_objects import (
    EMBEDDING_DIMENSION,
    EmbeddingVector,
)
from tests.logfire._metric_helpers import collected_metrics, sum_counter_for_result

_METRIC = "vector.agent.internal_retrieval.outcome"
_CACHE_METRIC = "vector.agent.internal_retrieval.query_embedding_cache"


def _vector(value: float = 0.1) -> EmbeddingVector:
    return EmbeddingVector(root=tuple([value] * EMBEDDING_DIMENSION))


def _queries(*queries: str) -> InternalSearchQueries:
    return InternalSearchQueries(queries=queries)


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


def _article_hit(
    *,
    curation_id: int,
    assessment_id: int | None = None,
    title: str,
    distance: float,
) -> InternalArticleSearchHit:
    article = InScopeAnalyzedArticle(
        curation_id=curation_id,
        title=title,
        summary=f"{title} summary",
        assessment_result=InScope(
            category=InScopeCategory.AI,
            investor_take="投資家視点",
            key_points=[],
        ),
    )
    return InternalArticleSearchHit(
        assessment_id=assessment_id or curation_id + 1000,
        article=article,
        content=InternalArticleContent.from_article(article, published_at=None),
        distance=distance,
    )


class FakeArticleVectorSearchRepository:
    def __init__(
        self,
        hits_by_query: dict[str, list[InternalArticleSearchHit]],
    ) -> None:
        self.hits_by_query = hits_by_query
        self.calls: list[tuple[InternalQueryEmbedding, int]] = []

    async def search_by_embedding(
        self,
        embedding: InternalQueryEmbedding,
        *,
        limit: int,
    ) -> list[InternalArticleSearchHit]:
        self.calls.append((embedding, limit))
        return list(self.hits_by_query.get(embedding.query, []))


class FakeQueryEmbeddingCache:
    def __init__(
        self,
        *,
        cached: dict[str, EmbeddingVector] | None = None,
        fetch_error: Exception | None = None,
        store_error: Exception | None = None,
    ) -> None:
        self.cached = cached or {}
        self.fetch_error = fetch_error
        self.store_error = store_error
        self.fetch_calls: list[InternalSearchQueries] = []
        self.store_calls: list[InternalQueryEmbedding] = []

    async def fetch_cached(
        self,
        queries: InternalSearchQueries,
    ) -> dict[str, EmbeddingVector]:
        self.fetch_calls.append(queries)
        if self.fetch_error is not None:
            raise self.fetch_error
        return {
            query: self.cached[query]
            for query in queries.queries
            if query in self.cached
        }

    async def store(self, embedding: InternalQueryEmbedding) -> None:
        self.store_calls.append(embedding)
        if self.store_error is not None:
            raise self.store_error


class FakeEventReporter:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def event_occurred(self, event: Any) -> None:
        self.events.append(event)


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
    async def test_embed_queries_embeds_normalized_queries(
        self,
        capfire: CaptureLogfire,
    ) -> None:
        embedder = FakeInternalQueryEmbedder()
        service = InternalSearchService(embedder=embedder)

        embeddings = await service.embed_queries(_queries("NVIDIA", "OpenAI", "Apple"))

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

    async def test_empty_embedder_result_records_empty_metric(
        self,
        capfire: CaptureLogfire,
    ) -> None:
        embedder = FakeInternalQueryEmbedder(empty_result=True)
        service = InternalSearchService(embedder=embedder)

        embeddings = await service.embed_queries(_queries("NVIDIA"))

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
            await service.embed_queries(_queries("NVIDIA secret query"))

        metrics = collected_metrics(capfire)
        assert sum_counter_for_result(metrics, _METRIC, "failed") == 1
        assert _metric_attributes(metrics, _METRIC) == [
            {"result": "failed", "query_count": 1}
        ]
        dumped = json.dumps(metrics, default=str, ensure_ascii=False)
        assert "NVIDIA secret query" not in dumped

    async def test_embed_queries_uses_cache_hit_without_embedder(self) -> None:
        embedder = FakeInternalQueryEmbedder()
        cache = FakeQueryEmbeddingCache(cached={"NVIDIA": _vector(0.8)})
        service = InternalSearchService(
            embedder=embedder,
            query_embedding_cache=cache,
        )

        embeddings = await service.embed_queries(_queries("NVIDIA"))

        assert [embedding.query for embedding in embeddings] == ["NVIDIA"]
        assert embeddings[0].vector.to_list()[0] == pytest.approx(0.8)
        assert embedder.calls == []
        assert [call.queries for call in cache.fetch_calls] == [("NVIDIA",)]
        assert cache.store_calls == []

    async def test_embed_queries_embeds_only_cache_misses_and_stores_them(
        self,
    ) -> None:
        embedder = FakeInternalQueryEmbedder()
        cache = FakeQueryEmbeddingCache(cached={"NVIDIA": _vector(0.8)})
        service = InternalSearchService(
            embedder=embedder,
            query_embedding_cache=cache,
        )

        embeddings = await service.embed_queries(_queries("NVIDIA", "OpenAI"))

        assert [embedding.query for embedding in embeddings] == ["NVIDIA", "OpenAI"]
        assert [call.queries for call in embedder.calls] == [("OpenAI",)]
        assert [stored.query for stored in cache.store_calls] == ["OpenAI"]

    async def test_cache_lookup_failure_does_not_stop_embedding(
        self,
        capfire: CaptureLogfire,
    ) -> None:
        embedder = FakeInternalQueryEmbedder()
        cache = FakeQueryEmbeddingCache(fetch_error=RuntimeError("db down"))
        service = InternalSearchService(
            embedder=embedder,
            query_embedding_cache=cache,
        )

        embeddings = await service.embed_queries(_queries("NVIDIA"))

        assert [embedding.query for embedding in embeddings] == ["NVIDIA"]
        assert [call.queries for call in embedder.calls] == [("NVIDIA",)]
        metrics = collected_metrics(capfire)
        assert sum_counter_for_result(metrics, _CACHE_METRIC, "lookup_failed") == 1

    async def test_cache_save_failure_does_not_drop_embedding(
        self,
        capfire: CaptureLogfire,
    ) -> None:
        embedder = FakeInternalQueryEmbedder()
        cache = FakeQueryEmbeddingCache(store_error=RuntimeError("db down"))
        service = InternalSearchService(
            embedder=embedder,
            query_embedding_cache=cache,
        )

        embeddings = await service.embed_queries(_queries("NVIDIA"))

        assert [embedding.query for embedding in embeddings] == ["NVIDIA"]
        assert [stored.query for stored in cache.store_calls] == ["NVIDIA"]
        metrics = collected_metrics(capfire)
        assert sum_counter_for_result(metrics, _CACHE_METRIC, "save_failed") == 1

    async def test_search_articles_searches_with_embedded_internal_queries(
        self,
    ) -> None:
        embedder = FakeInternalQueryEmbedder()
        search_repo = FakeArticleVectorSearchRepository(
            {
                "NVIDIA": [
                    _article_hit(curation_id=1, title="NVIDIA記事", distance=0.1)
                ],
                "OpenAI": [
                    _article_hit(curation_id=2, title="OpenAI記事", distance=0.2)
                ],
            }
        )
        service = InternalSearchService(
            embedder=embedder,
            article_search_repository=search_repo,
        )

        hits = await service.search_articles(
            _queries("NVIDIA", "OpenAI"),
            per_query_limit=4,
            limit=5,
        )

        assert [hit.article.title for hit in hits] == ["NVIDIA記事", "OpenAI記事"]
        assert [(call.query, limit) for call, limit in search_repo.calls] == [
            ("NVIDIA", 4),
            ("OpenAI", 4),
        ]

    async def test_search_articles_reports_counts_without_query_text(self) -> None:
        reporter = FakeEventReporter()
        embedder = FakeInternalQueryEmbedder()
        search_repo = FakeArticleVectorSearchRepository(
            {
                "SECRET raw user question": [
                    _article_hit(curation_id=1, title="NVIDIA記事", distance=0.1)
                ],
            }
        )
        service = InternalSearchService(
            embedder=embedder,
            article_search_repository=search_repo,
            events=reporter,
        )

        await service.search_articles(_queries("SECRET raw user question"))

        assert [event.type for event in reporter.events] == [
            "internal_search.started",
            "internal_search.completed",
        ]
        assert reporter.events[0].query_count == 1
        assert reporter.events[1].hit_count == 1
        serialized = json.dumps(
            [event.model_dump(mode="json") for event in reporter.events],
            ensure_ascii=False,
        )
        assert "SECRET raw user question" not in serialized

    @pytest.mark.parametrize("kwargs", [{"limit": 0}, {"per_query_limit": 0}])
    async def test_search_articles_limit_guard_returns_without_events(
        self,
        kwargs: dict[str, int],
    ) -> None:
        reporter = FakeEventReporter()
        search_repo = FakeArticleVectorSearchRepository({})
        service = InternalSearchService(
            embedder=FakeInternalQueryEmbedder(),
            article_search_repository=search_repo,
            events=reporter,
        )

        hits = await service.search_articles(_queries("NVIDIA"), **kwargs)

        assert hits == []
        assert search_repo.calls == []
        assert reporter.events == []

    async def test_search_articles_reports_zero_hits_when_embeddings_are_empty(
        self,
    ) -> None:
        reporter = FakeEventReporter()
        service = InternalSearchService(
            embedder=FakeInternalQueryEmbedder(empty_result=True),
            article_search_repository=FakeArticleVectorSearchRepository({}),
            events=reporter,
        )

        hits = await service.search_articles(_queries("SECRET fallback question"))

        assert hits == []
        assert [event.type for event in reporter.events] == [
            "internal_search.started",
            "internal_search.completed",
        ]
        assert reporter.events[0].query_count == 1
        assert reporter.events[1].hit_count == 0

    async def test_search_articles_dedupes_by_curation_id_with_min_distance(
        self,
    ) -> None:
        embedder = FakeInternalQueryEmbedder()
        search_repo = FakeArticleVectorSearchRepository(
            {
                "NVIDIA": [
                    _article_hit(curation_id=1, title="重複記事 遠い", distance=0.4),
                    _article_hit(curation_id=2, title="別記事", distance=0.2),
                ],
                "OpenAI": [
                    _article_hit(curation_id=1, title="重複記事 近い", distance=0.1)
                ],
            }
        )
        service = InternalSearchService(
            embedder=embedder,
            article_search_repository=search_repo,
        )

        hits = await service.search_articles(
            _queries("NVIDIA", "OpenAI"),
            limit=10,
        )

        assert [(hit.article.curation_id, hit.article.title) for hit in hits] == [
            (1, "重複記事 近い"),
            (2, "別記事"),
        ]
        assert [hit.distance for hit in hits] == [0.1, 0.2]
