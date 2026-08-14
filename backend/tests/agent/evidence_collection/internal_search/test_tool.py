"""Internal search tool query embedding tests."""

from __future__ import annotations

import json
from dataclasses import fields, is_dataclass
from typing import Any, Literal, get_args, get_origin, get_type_hints
from unittest.mock import Mock

import pytest
from logfire.testing import CaptureLogfire

import app.agent.evidence_collection.internal_search.tool as tool_module
from app.agent.evidence_collection.internal_search import (
    InternalArticleContent,
    InternalArticleSearchHit,
)
from app.agent.evidence_collection.internal_search.contract import (
    INTERNAL_SEARCH_TOOL_NAME,
    InternalSearchError,
    InternalSearchTool,
    InternalSearchToolInput,
)
from app.agent.evidence_collection.internal_search.query_embedding import (
    InternalQueryEmbedding,
    InternalSearchQueries,
)
from app.agent.evidence_collection.internal_search.tool import (
    PgVectorInternalSearchTool,
)
from app.analysis.ai_provider_errors import AIProviderError
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
        *,
        error: Exception | None = None,
    ) -> None:
        self.hits_by_query = hits_by_query
        self.error = error
        self.calls: list[tuple[InternalQueryEmbedding, int]] = []

    async def search_by_embedding(
        self,
        embedding: InternalQueryEmbedding,
        *,
        limit: int,
    ) -> list[InternalArticleSearchHit]:
        self.calls.append((embedding, limit))
        if self.error is not None:
            raise self.error
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


class TestPgVectorInternalSearchTool:
    async def test_embed_queries_embeds_normalized_queries(
        self,
        capfire: CaptureLogfire,
    ) -> None:
        embedder = FakeInternalQueryEmbedder()
        service = PgVectorInternalSearchTool(embedder=embedder)

        embeddings = await service.embed_queries(_queries("NVIDIA", "OpenAI", "Apple"))

        assert [embedding.query for embedding in embeddings] == [
            "NVIDIA",
            "OpenAI",
            "Apple",
        ]
        assert [call.queries for call in embedder.calls] == [
            ("NVIDIA", "OpenAI", "Apple")
        ]
        # outcome metric の所有者は invoke 境界。embed_queries 単体では
        # どのラベルでも emit しない (二重計上防止)。
        assert _metric_attributes(collected_metrics(capfire), _METRIC) == []

    async def test_empty_embedder_result_does_not_emit_outcome_metric(
        self,
        capfire: CaptureLogfire,
    ) -> None:
        embedder = FakeInternalQueryEmbedder(empty_result=True)
        service = PgVectorInternalSearchTool(embedder=embedder)

        embeddings = await service.embed_queries(_queries("NVIDIA"))

        assert embeddings == []
        assert _metric_attributes(collected_metrics(capfire), _METRIC) == []

    async def test_embedder_failure_does_not_emit_outcome_metric_or_leak_query(
        self,
        capfire: CaptureLogfire,
    ) -> None:
        embedder = FakeInternalQueryEmbedder(error=RuntimeError("embedder down"))
        service = PgVectorInternalSearchTool(embedder=embedder)

        with pytest.raises(RuntimeError, match="embedder down"):
            await service.embed_queries(_queries("NVIDIA secret query"))

        metrics = collected_metrics(capfire)
        assert _metric_attributes(metrics, _METRIC) == []
        dumped = json.dumps(metrics, default=str, ensure_ascii=False)
        assert "NVIDIA secret query" not in dumped

    async def test_embed_queries_uses_cache_hit_without_embedder(self) -> None:
        embedder = FakeInternalQueryEmbedder()
        cache = FakeQueryEmbeddingCache(cached={"NVIDIA": _vector(0.8)})
        service = PgVectorInternalSearchTool(
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
        service = PgVectorInternalSearchTool(
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
        service = PgVectorInternalSearchTool(
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
        service = PgVectorInternalSearchTool(
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
        capfire: CaptureLogfire,
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
        service = PgVectorInternalSearchTool(
            embedder=embedder,
            article_search_repository=search_repo,
        )

        hits = await service.invoke(
            InternalSearchToolInput(queries=_queries("NVIDIA", "OpenAI"))
        )

        assert [hit.article.title for hit in hits] == ["NVIDIA記事", "OpenAI記事"]
        # invoke()は既定のper_query_limit(5)を使う。
        assert [(call.query, limit) for call, limit in search_repo.calls] == [
            ("NVIDIA", 5),
            ("OpenAI", 5),
        ]
        assert _metric_attributes(collected_metrics(capfire), _METRIC) == [
            {"result": "succeeded", "query_count": 2}
        ]

    async def test_search_articles_empty_result_records_overall_empty_metric(
        self,
        capfire: CaptureLogfire,
    ) -> None:
        service = PgVectorInternalSearchTool(
            embedder=FakeInternalQueryEmbedder(),
            article_search_repository=FakeArticleVectorSearchRepository({}),
        )

        hits = await service.invoke(InternalSearchToolInput(queries=_queries("NVIDIA")))

        assert hits == []
        assert _metric_attributes(collected_metrics(capfire), _METRIC) == [
            {"result": "empty", "query_count": 1}
        ]

    async def test_search_articles_wraps_provider_failure_and_records_phase(
        self,
        capfire: CaptureLogfire,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        warning = Mock()
        monkeypatch.setattr(tool_module.logger, "warning", warning)
        provider_error = AIProviderError("SECRET provider message")
        service = PgVectorInternalSearchTool(
            embedder=FakeInternalQueryEmbedder(error=provider_error),
            article_search_repository=FakeArticleVectorSearchRepository({}),
        )

        with pytest.raises(InternalSearchError) as captured:
            await service.invoke(
                InternalSearchToolInput(queries=_queries("SECRET raw user question"))
            )

        assert captured.value.phase == "query_embedding"
        assert captured.value.__cause__ is provider_error
        attributes = _metric_attributes(collected_metrics(capfire), _METRIC)
        assert attributes == [
            {
                "result": "failed",
                "query_count": 1,
                "failure_phase": "query_embedding",
            }
        ]
        assert "SECRET raw user question" not in json.dumps(
            attributes, ensure_ascii=False
        )
        warning.assert_called_once_with(
            "internal_search_failed",
            failure_phase="query_embedding",
            query_count=1,
        )
        assert "SECRET" not in repr(warning.call_args)

    async def test_search_articles_classified_repository_failure_records_phase(
        self,
        capfire: CaptureLogfire,
    ) -> None:
        repository_error = InternalSearchError(phase="article_search")
        service = PgVectorInternalSearchTool(
            embedder=FakeInternalQueryEmbedder(),
            article_search_repository=FakeArticleVectorSearchRepository(
                {}, error=repository_error
            ),
        )

        with pytest.raises(InternalSearchError) as captured:
            await service.invoke(
                InternalSearchToolInput(queries=_queries("SECRET raw user question"))
            )

        assert captured.value is repository_error
        assert _metric_attributes(collected_metrics(capfire), _METRIC) == [
            {
                "result": "failed",
                "query_count": 1,
                "failure_phase": "article_search",
            }
        ]

    async def test_search_articles_unclassified_failure_records_unknown_and_propagates(
        self,
        capfire: CaptureLogfire,
    ) -> None:
        service = PgVectorInternalSearchTool(
            embedder=FakeInternalQueryEmbedder(),
            article_search_repository=FakeArticleVectorSearchRepository(
                {}, error=RuntimeError("repository bug")
            ),
        )

        with pytest.raises(RuntimeError, match="repository bug"):
            await service.invoke(
                InternalSearchToolInput(queries=_queries("SECRET raw user question"))
            )

        assert _metric_attributes(collected_metrics(capfire), _METRIC) == [
            {
                "result": "failed",
                "query_count": 1,
                "failure_phase": "unknown",
            }
        ]

    async def test_invoke_returns_hits_through_tool_port_without_event_reporter(
        self,
    ) -> None:
        embedder = FakeInternalQueryEmbedder()
        search_repo = FakeArticleVectorSearchRepository(
            {
                "NVIDIA": [
                    _article_hit(curation_id=1, title="NVIDIA記事", distance=0.1)
                ],
            }
        )
        service = PgVectorInternalSearchTool(
            embedder=embedder,
            article_search_repository=search_repo,
        )

        hits = await service.invoke(InternalSearchToolInput(queries=_queries("NVIDIA")))

        assert [hit.article.title for hit in hits] == ["NVIDIA記事"]

    @pytest.mark.parametrize("kwargs", [{"limit": 0}, {"per_query_limit": 0}])
    async def test_search_articles_limit_guard_returns_empty_without_calling_repository(
        self,
        kwargs: dict[str, int],
    ) -> None:
        """limit/per_query_limitはinvoke()から到達不能な実装policyのため直接検証する。"""
        search_repo = FakeArticleVectorSearchRepository({})
        service = PgVectorInternalSearchTool(
            embedder=FakeInternalQueryEmbedder(),
            article_search_repository=search_repo,
        )

        hits = await service._search_articles(_queries("NVIDIA"), **kwargs)

        assert hits == []
        assert search_repo.calls == []

    async def test_search_articles_returns_empty_hits_when_embeddings_are_empty(
        self,
    ) -> None:
        service = PgVectorInternalSearchTool(
            embedder=FakeInternalQueryEmbedder(empty_result=True),
            article_search_repository=FakeArticleVectorSearchRepository({}),
        )

        hits = await service.invoke(
            InternalSearchToolInput(queries=_queries("SECRET fallback question"))
        )

        assert hits == []

    def test_service_has_no_progress_event_reporter_field(self) -> None:
        assert "events" not in {
            field.name for field in fields(PgVectorInternalSearchTool)
        }
        with pytest.raises(TypeError):
            PgVectorInternalSearchTool(  # type: ignore[call-arg]
                embedder=FakeInternalQueryEmbedder(),
                events=object(),
            )

    def test_internal_search_tool_is_stably_typed_like_external_search_tool(
        self,
    ) -> None:
        assert is_dataclass(InternalSearchToolInput)
        assert InternalSearchToolInput.__dataclass_params__.frozen
        assert "__slots__" in InternalSearchToolInput.__dict__
        assert [field.name for field in fields(InternalSearchToolInput)] == ["queries"]
        assert get_type_hints(InternalSearchToolInput) == {
            "queries": InternalSearchQueries
        }
        assert get_type_hints(InternalSearchTool.invoke) == {
            "input": InternalSearchToolInput,
            "return": list[InternalArticleSearchHit],
        }
        name_property = InternalSearchTool.__dict__["name"]
        name_type = get_type_hints(name_property.fget)["return"]
        assert get_origin(name_type) is Literal
        assert get_args(name_type) == ("internal_search",)
        assert INTERNAL_SEARCH_TOOL_NAME == "internal_search"

        service = PgVectorInternalSearchTool(embedder=FakeInternalQueryEmbedder())
        assert service.name == "internal_search"

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
        service = PgVectorInternalSearchTool(
            embedder=embedder,
            article_search_repository=search_repo,
        )

        # dedup後は2件のみのため既定limit(5)に収まり、invoke()経由で検証できる。
        hits = await service.invoke(
            InternalSearchToolInput(queries=_queries("NVIDIA", "OpenAI"))
        )

        assert [(hit.article.curation_id, hit.article.title) for hit in hits] == [
            (1, "重複記事 近い"),
            (2, "別記事"),
        ]
        assert [hit.distance for hit in hits] == [0.1, 0.2]
