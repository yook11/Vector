"""Gemini query embedder tests for internal retrieval."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import httpx2
import pytest
from google.genai import errors as genai_errors

from app.agent.evidence_collection.internal_search.ai.gemini import GeminiQueryEmbedder
from app.agent.evidence_collection.internal_search.ai.gemini_spec import (
    GEMINI_QUERY_EMBEDDING_SPEC,
)
from app.agent.evidence_collection.internal_search.query_embedding import (
    InternalQueryEmbedding,
    InternalSearchQueries,
)
from app.ai_providers.errors import (
    AIProviderResponseError,
    AIProviderResultError,
    AIProviderResultReason,
    AIProviderTransportError,
)
from app.analysis.embedding.domain.value_objects import (
    EMBEDDING_DIMENSION,
    EmbeddingVector,
)
from tests.cloudwatch.records import metric_records


class _ClientScope:
    """embed_content を差し替えた client を貸し、開閉を記録する。"""

    def __init__(self, embed_content: AsyncMock) -> None:
        self.client = MagicMock()
        self.client.models.embed_content = embed_content
        self.events: list[str] = []

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[MagicMock]:
        self.events.append("open")
        try:
            yield self.client
        finally:
            self.events.append("close")


def _make_embedder(embed_content: AsyncMock | None = None) -> GeminiQueryEmbedder:
    return GeminiQueryEmbedder(
        client_scope_factory=_ClientScope(embed_content or AsyncMock())
    )


def _make_embed_response(vectors: list[list[float] | None]) -> MagicMock:
    response = MagicMock()
    response.embeddings = [MagicMock(values=vector) for vector in vectors]
    return response


def _api_error(
    code: int,
    status: str,
    message: str = "msg",
) -> genai_errors.ClientError:
    response_json = {"error": {"status": status, "message": message}}
    return genai_errors.ClientError(code, response_json)


def test_spec_is_query_embedding_spec_singleton() -> None:
    assert GeminiQueryEmbedder.SPEC is GEMINI_QUERY_EMBEDDING_SPEC


def test_property_contracts_return_spec_values() -> None:
    embedder = _make_embedder()

    assert embedder.model_name == GEMINI_QUERY_EMBEDDING_SPEC.model
    assert embedder.dimension == GEMINI_QUERY_EMBEDDING_SPEC.dimension


async def test_embed_queries_uses_retrieval_query_task_type() -> None:
    mock_call = AsyncMock(
        return_value=_make_embed_response(
            [
                [0.1] * EMBEDDING_DIMENSION,
                [0.2] * EMBEDDING_DIMENSION,
            ]
        )
    )
    embedder = _make_embedder(mock_call)

    result = await embedder.embed_queries(
        InternalSearchQueries(queries=("NVIDIA", "OpenAI"))
    )

    assert [embedding.query for embedding in result] == ["NVIDIA", "OpenAI"]
    assert all(isinstance(embedding, InternalQueryEmbedding) for embedding in result)
    assert all(isinstance(embedding.vector, EmbeddingVector) for embedding in result)
    config = mock_call.call_args.kwargs["config"]
    assert config.task_type == GEMINI_QUERY_EMBEDDING_SPEC.task_type
    assert config.output_dimensionality == (
        GEMINI_QUERY_EMBEDDING_SPEC.output_dimensionality
    )
    assert mock_call.call_args.kwargs["model"] == GEMINI_QUERY_EMBEDDING_SPEC.model
    assert mock_call.call_args.kwargs["contents"] == ["NVIDIA", "OpenAI"]


async def test_embed_queries_opens_and_closes_client_for_each_call() -> None:
    scope = _ClientScope(
        AsyncMock(return_value=_make_embed_response([[0.1] * EMBEDDING_DIMENSION]))
    )
    embedder = GeminiQueryEmbedder(client_scope_factory=scope)

    for _ in range(2):
        await embedder.embed_queries(InternalSearchQueries(queries=("NVIDIA",)))

    assert scope.events == ["open", "close", "open", "close"]


async def test_embed_queries_closes_client_when_provider_call_fails() -> None:
    scope = _ClientScope(AsyncMock(side_effect=_api_error(429, "RESOURCE_EXHAUSTED")))
    embedder = GeminiQueryEmbedder(client_scope_factory=scope)

    with pytest.raises(AIProviderResponseError):
        await embedder.embed_queries(InternalSearchQueries(queries=("NVIDIA",)))

    assert scope.events == ["open", "close"]


async def test_embed_queries_skips_api_for_empty_queries() -> None:
    scope = _ClientScope(AsyncMock())
    embedder = GeminiQueryEmbedder(client_scope_factory=scope)

    result = await embedder.embed_queries(InternalSearchQueries())

    assert result == []
    assert scope.events == []
    scope.client.models.embed_content.assert_not_called()


async def test_embed_queries_raises_generation_error_when_embeddings_empty() -> None:
    response = MagicMock()
    response.embeddings = []
    embedder = _make_embedder(AsyncMock(return_value=response))

    with pytest.raises(AIProviderResultError) as exc_info:
        await embedder.embed_queries(InternalSearchQueries(queries=("NVIDIA",)))

    assert exc_info.value.reason is AIProviderResultReason.EMBEDDINGS_EMPTY


async def test_embed_queries_raises_generation_error_when_values_missing() -> None:
    embedder = _make_embedder(AsyncMock(return_value=_make_embed_response([None])))

    with pytest.raises(AIProviderResultError) as exc_info:
        await embedder.embed_queries(InternalSearchQueries(queries=("NVIDIA",)))

    assert exc_info.value.reason is AIProviderResultReason.EMBEDDING_VALUES_MISSING


async def test_embed_queries_raises_generation_error_on_count_mismatch() -> None:
    embedder = _make_embedder(
        AsyncMock(return_value=_make_embed_response([[0.1] * EMBEDDING_DIMENSION]))
    )

    with pytest.raises(AIProviderResultError) as exc_info:
        await embedder.embed_queries(
            InternalSearchQueries(queries=("NVIDIA", "OpenAI"))
        )

    assert exc_info.value.reason is AIProviderResultReason.EMBEDDING_COUNT_MISMATCH


def test_delegates_timeout_to_transport_error() -> None:
    embedder = _make_embedder()
    result = embedder._translate_error(httpx2.ReadTimeout("deadline"))

    assert isinstance(result, AIProviderTransportError)


async def test_embed_queries_translates_rate_limited_error() -> None:
    embedder = _make_embedder(
        AsyncMock(side_effect=_api_error(429, "RESOURCE_EXHAUSTED"))
    )

    with pytest.raises(AIProviderResponseError):
        await embedder.embed_queries(InternalSearchQueries(queries=("NVIDIA",)))


# ai_provider_exhausted EMF emit (CloudWatch A6) — _embed_once の except 節は
# runtime を経由しない唯一の provider 呼出し経路なので、枯渇系翻訳の emit を単独で守る。


_EXHAUSTED_METRIC = "ai_provider_exhausted"
_PER_DAY_QUOTA_ID = "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
_PER_MINUTE_QUOTA_ID = "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"


def _resource_exhausted_error(*quota_ids: str) -> genai_errors.ClientError:
    """429 RESOURCE_EXHAUSTED を構造化 details (QuotaFailure) 付きで構築する。"""
    error_body: dict = {
        "status": "RESOURCE_EXHAUSTED",
        "message": "You exceeded your current quota, check your plan and billing.",
    }
    if quota_ids:
        error_body["details"] = [
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{"quotaId": quota_id} for quota_id in quota_ids],
            }
        ]
    return genai_errors.ClientError(429, {"error": error_body})


async def test_embed_queries_quota_exhausted_emits_ai_provider_exhausted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """per-day quota 超過は usage_limit_exhausted 翻訳で provider=gemini を emit。"""
    embedder = _make_embedder(
        AsyncMock(side_effect=_resource_exhausted_error(_PER_DAY_QUOTA_ID))
    )

    with pytest.raises(AIProviderResponseError):
        await embedder.embed_queries(InternalSearchQueries(queries=("NVIDIA",)))

    records = metric_records(capsys.readouterr().out, _EXHAUSTED_METRIC)
    assert len(records) == 1
    assert records[0]["kind"] == "quota_exhausted"
    assert records[0]["provider"] == "gemini"


async def test_embed_queries_per_minute_rate_limited_does_not_emit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """per-minute バーストは rate limited に翻訳され、枯渇としては emit しない。"""
    embedder = _make_embedder(
        AsyncMock(side_effect=_resource_exhausted_error(_PER_MINUTE_QUOTA_ID))
    )

    with pytest.raises(AIProviderResponseError):
        await embedder.embed_queries(InternalSearchQueries(queries=("NVIDIA",)))

    assert metric_records(capsys.readouterr().out, _EXHAUSTED_METRIC) == []
