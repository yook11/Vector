"""Gemini query embedder tests for internal retrieval."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.genai import errors as genai_errors

from app.agent.internal_retrieval.ai.gemini import GeminiQueryEmbedder
from app.agent.internal_retrieval.ai.gemini_spec import (
    GEMINI_QUERY_EMBEDDING_SPEC,
)
from app.agent.internal_retrieval.query_embedding import (
    InternalQueryEmbedding,
    InternalSearchQueries,
)
from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderNetworkError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
)
from app.analysis.embedding.domain.value_objects import (
    EMBEDDING_DIMENSION,
    EmbeddingVector,
)
from app.analysis.gemini_error_translator import GeminiStateReason


def _make_embedder() -> GeminiQueryEmbedder:
    with (
        patch("app.agent.internal_retrieval.ai.gemini.genai.Client"),
        patch("app.agent.internal_retrieval.ai.gemini.settings") as mock_settings,
    ):
        mock_settings.gemini_api_key.get_secret_value.return_value = "test-key"
        return GeminiQueryEmbedder()


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


def test_init_raises_configuration_error_when_api_key_missing() -> None:
    with patch("app.agent.internal_retrieval.ai.gemini.settings") as mock_settings:
        mock_settings.gemini_api_key.get_secret_value.return_value = ""

        with pytest.raises(AIProviderConfigurationError) as exc_info:
            GeminiQueryEmbedder()

    assert exc_info.value.reason is GeminiStateReason.NOT_CONFIGURED


def test_spec_is_query_embedding_spec_singleton() -> None:
    assert GeminiQueryEmbedder.SPEC is GEMINI_QUERY_EMBEDDING_SPEC


def test_property_contracts_return_spec_values() -> None:
    embedder = _make_embedder()

    assert embedder.model_name == GEMINI_QUERY_EMBEDDING_SPEC.model
    assert embedder.dimension == GEMINI_QUERY_EMBEDDING_SPEC.dimension
    assert embedder.rate_limit_policy == GEMINI_QUERY_EMBEDDING_SPEC.rate_limit_policy


async def test_embed_queries_uses_retrieval_query_task_type() -> None:
    embedder = _make_embedder()
    mock_call = AsyncMock(
        return_value=_make_embed_response(
            [
                [0.1] * EMBEDDING_DIMENSION,
                [0.2] * EMBEDDING_DIMENSION,
            ]
        )
    )
    embedder._client.aio.models.embed_content = mock_call

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


async def test_embed_queries_skips_api_for_empty_queries() -> None:
    embedder = _make_embedder()
    embedder._client.aio.models.embed_content = AsyncMock()

    result = await embedder.embed_queries(InternalSearchQueries())

    assert result == []
    embedder._client.aio.models.embed_content.assert_not_called()


async def test_embed_queries_raises_request_invalid_when_embeddings_empty() -> None:
    embedder = _make_embedder()
    response = MagicMock()
    response.embeddings = []
    embedder._client.aio.models.embed_content = AsyncMock(return_value=response)

    with pytest.raises(AIProviderRequestInvalidError) as exc_info:
        await embedder.embed_queries(InternalSearchQueries(queries=("NVIDIA",)))

    assert exc_info.value.reason is GeminiStateReason.EMPTY_EMBEDDINGS


async def test_embed_queries_raises_request_invalid_when_values_missing() -> None:
    embedder = _make_embedder()
    embedder._client.aio.models.embed_content = AsyncMock(
        return_value=_make_embed_response([None])
    )

    with pytest.raises(AIProviderRequestInvalidError) as exc_info:
        await embedder.embed_queries(InternalSearchQueries(queries=("NVIDIA",)))

    assert exc_info.value.reason is GeminiStateReason.MISSING_VALUES


async def test_embed_queries_raises_request_invalid_on_count_mismatch() -> None:
    embedder = _make_embedder()
    embedder._client.aio.models.embed_content = AsyncMock(
        return_value=_make_embed_response([[0.1] * EMBEDDING_DIMENSION])
    )

    with pytest.raises(AIProviderRequestInvalidError) as exc_info:
        await embedder.embed_queries(
            InternalSearchQueries(queries=("NVIDIA", "OpenAI"))
        )

    assert exc_info.value.reason is GeminiStateReason.EMBEDDING_COUNT_MISMATCH


def test_delegates_timeout_to_network_error() -> None:
    embedder = _make_embedder()
    result = embedder._translate_error(TimeoutError("deadline"))

    assert isinstance(result, AIProviderNetworkError)


async def test_embed_queries_translates_rate_limited_error() -> None:
    embedder = _make_embedder()
    embedder._client.aio.models.embed_content = AsyncMock(
        side_effect=_api_error(429, "RESOURCE_EXHAUSTED")
    )

    with pytest.raises(AIProviderRateLimitedError):
        await embedder.embed_queries(InternalSearchQueries(queries=("NVIDIA",)))
