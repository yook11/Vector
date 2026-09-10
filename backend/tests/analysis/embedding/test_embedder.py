"""借用クライアントからの要求・応答・例外変換を検証する。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from google.genai import errors, types
from pydantic import SecretStr

from app.ai_providers.errors import (
    AIProviderConfigurationError,
    AIProviderNetworkError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
    AIProviderServiceUnavailableError,
    AIProviderUsageLimitExhaustedError,
)
from app.ai_providers.gemini import client as client_module
from app.ai_providers.gemini.settings import GeminiConnectionSettings
from app.analysis.embedding.domain.ready import ReadyForEmbedding
from app.analysis.embedding.domain.value_objects import EMBEDDING_DIMENSION
from app.analysis.embedding.embedder import GeminiEmbedder
from app.analysis.embedding.errors import EmbeddingResponseInvalidError


@pytest.fixture
def ready():
    return ReadyForEmbedding(analyzed_article_id=1, text_for_embedding="private text")


@pytest.fixture
def sdk_client():
    return SimpleNamespace(
        models=SimpleNamespace(
            embed_content=AsyncMock(
                return_value=types.EmbedContentResponse(
                    embeddings=[
                        types.ContentEmbedding(values=[0.2] * EMBEDDING_DIMENSION)
                    ]
                )
            )
        ),
        aclose=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_request_and_valid_vector(sdk_client, ready):
    embedder = GeminiEmbedder(client=sdk_client)
    result = await embedder.embed_document(ready)
    assert result.to_list() == [0.2] * EMBEDDING_DIMENSION
    assert (
        embedder.model_name,
        embedder.dimension,
        embedder.provider,
        embedder.document_prefix,
    ) == ("gemini-embedding-001", 768, "gemini", "")
    sdk_client.models.embed_content.assert_awaited_once()
    kwargs = sdk_client.models.embed_content.call_args.kwargs
    assert kwargs["model"] == "gemini-embedding-001"
    assert kwargs["contents"] == ready.text_for_embedding
    assert kwargs["config"].output_dimensionality == 768
    assert kwargs["config"].task_type == "RETRIEVAL_DOCUMENT"
    sdk_client.aclose.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "embeddings,reason",
    [
        (None, "empty_embeddings"),
        ([], "empty_embeddings"),
        ([types.ContentEmbedding()], "missing_values"),
    ],
)
async def test_missing_response_fields(sdk_client, ready, embeddings, reason):
    sdk_client.models.embed_content.return_value = types.EmbedContentResponse(
        embeddings=embeddings
    )
    with pytest.raises(AIProviderRequestInvalidError) as caught:
        await GeminiEmbedder(client=sdk_client).embed_document(ready)
    assert caught.value.reason == reason
    sdk_client.aclose.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "values",
    [
        [],
        [0.2] * 767,
        [float("nan")] * 768,
        [float("inf")] * 768,
        [float("-inf")] * 768,
        [10001.0] * 768,
    ],
)
async def test_invalid_vector(sdk_client, ready, values):
    sdk_client.models.embed_content.return_value = types.EmbedContentResponse(
        embeddings=[types.ContentEmbedding(values=values)]
    )
    with pytest.raises(EmbeddingResponseInvalidError):
        await GeminiEmbedder(client=sdk_client).embed_document(ready)
    sdk_client.aclose.assert_not_called()


@pytest.mark.asyncio
async def test_uses_first_embedding_and_can_reuse_after_failure(sdk_client, ready):
    embedder = GeminiEmbedder(client=sdk_client)
    sdk_client.models.embed_content.side_effect = [
        RuntimeError("private"),
        types.EmbedContentResponse(
            embeddings=[
                types.ContentEmbedding(values=[0.3] * 768),
                types.ContentEmbedding(values=[]),
            ]
        ),
    ]
    with pytest.raises(RuntimeError):
        await embedder.embed_document(ready)
    assert (await embedder.embed_document(ready)).to_list() == [0.3] * 768
    sdk_client.aclose.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,expected,reason",
    [
        (
            errors.ClientError(429, {"error": {"code": 429, "message": "private"}}),
            AIProviderRateLimitedError,
            "rate_limited",
        ),
        (
            errors.ClientError(
                429,
                {
                    "error": {
                        "code": 429,
                        "details": [
                            {
                                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                "violations": [{"quotaId": "RequestsPerDay"}],
                            }
                        ],
                    }
                },
            ),
            AIProviderUsageLimitExhaustedError,
            "quota_exhausted",
        ),
        (
            errors.ServerError(503, {"error": {"code": 503}}),
            AIProviderServiceUnavailableError,
            "server_error",
        ),
        (
            errors.ClientError(401, {"error": {"code": 401}}),
            AIProviderConfigurationError,
            "auth",
        ),
        (httpx.ReadTimeout("private"), AIProviderNetworkError, "timeout"),
        (httpx.ConnectError("private"), AIProviderNetworkError, "connection"),
    ],
)
async def test_provider_error_mapping(sdk_client, ready, error, expected, reason):
    sdk_client.models.embed_content.side_effect = error
    with pytest.raises(expected) as caught:
        await GeminiEmbedder(client=sdk_client).embed_document(ready)
    assert caught.value.reason == reason
    assert caught.value.__cause__ is error
    sdk_client.models.embed_content.assert_awaited_once()
    sdk_client.aclose.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("private"), asyncio.CancelledError()])
async def test_unknown_error_and_cancellation_propagate(sdk_client, ready, error):
    sdk_client.models.embed_content.side_effect = error
    with pytest.raises(type(error)) as caught:
        await GeminiEmbedder(client=sdk_client).embed_document(ready)
    assert caught.value is error
    sdk_client.aclose.assert_not_called()


@pytest.mark.asyncio
async def test_common_client_real_sdk_and_embedder(monkeypatch, ready):
    requests = []
    http_clients = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"embeddings": [{"values": [0.2] * 768}]})

    def factory(**kwargs):
        assert kwargs.pop("retries") == 0
        http = httpx.AsyncClient(transport=httpx.MockTransport(respond), **kwargs)
        http_clients.append(http)
        return http

    monkeypatch.setattr(client_module, "make_external_async_client", factory)
    async with client_module.open_gemini_client(
        api_key=SecretStr("test-key"), settings=GeminiConnectionSettings()
    ) as sdk_client:
        result = await GeminiEmbedder(client=sdk_client).embed_document(ready)
        assert len(result) == 768
        assert not http_clients[0].is_closed
    assert http_clients[0].is_closed
    assert len(requests) == 1
    request = requests[0]
    assert request.url.host == "generativelanguage.googleapis.com"
    assert request.extensions["timeout"] == {
        "connect": 3,
        "read": 10,
        "write": 10,
        "pool": 3,
    }
    body = json.loads(request.content)
    assert body["requests"][0]["content"]["parts"] == [
        {"text": ready.text_for_embedding}
    ]
    assert body["requests"][0]["taskType"] == "RETRIEVAL_DOCUMENT"
    assert body["requests"][0]["outputDimensionality"] == 768
