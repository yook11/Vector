"""GeminiEmbedder のテスト。

API 呼び出しは google-genai client をモックする。エラーマッピングは
``_translate_error`` を直接呼び出して構造的に検証する。
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.genai.errors import APIError, ServerError

from app.analysis.embedder.gemini import GeminiEmbedder
from app.analysis.errors import (
    ConfigurationError,
    InvalidInputError,
    NetworkError,
    ProviderError,
    RateLimitError,
    UnclassifiedError,
)


def _make_embedder() -> GeminiEmbedder:
    """genai.Client を mock した GeminiEmbedder を返す。"""
    with (
        patch("app.analysis.embedder.gemini.genai.Client"),
        patch("app.analysis.embedder.gemini.settings") as mock_settings,
    ):
        mock_settings.gemini_api_key.get_secret_value.return_value = "test-key"
        return GeminiEmbedder()


def _make_embed_response(vectors: list[list[float]]) -> MagicMock:
    """EmbedContentResponse 互換のモックを返す。"""
    embeddings = [MagicMock(values=v) for v in vectors]
    response = MagicMock()
    response.embeddings = embeddings
    return response


# ---------------------------------------------------------------------------
# A. Initialization
# ---------------------------------------------------------------------------


def test_init_raises_configuration_error_when_api_key_missing() -> None:
    """API key が空文字なら ConfigurationError で初期化失敗。"""
    with patch("app.analysis.embedder.gemini.settings") as mock_settings:
        mock_settings.gemini_api_key.get_secret_value.return_value = ""
        with pytest.raises(ConfigurationError, match="GEMINI_API_KEY"):
            GeminiEmbedder()


def test_classvars_are_set() -> None:
    """ClassVar の MODEL / DIMENSION が公開仕様どおり。"""
    assert GeminiEmbedder.MODEL == "gemini-embedding-001"
    assert GeminiEmbedder.DIMENSION == 768
    assert GeminiEmbedder.RPM is None
    assert GeminiEmbedder.RPD is None
    assert GeminiEmbedder.DOCUMENT_PREFIX == ""
    assert GeminiEmbedder.QUERY_PREFIX == ""


# ---------------------------------------------------------------------------
# B. embed_document / embed_query / embed_documents — task_type 経路
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_document_uses_retrieval_document_task_type() -> None:
    embedder = _make_embedder()
    mock_call = AsyncMock(return_value=_make_embed_response([[0.1] * 768]))
    embedder._client.aio.models.embed_content = mock_call

    result = await embedder.embed_document("hello")

    assert result == [0.1] * 768
    assert mock_call.call_count == 1
    config = mock_call.call_args.kwargs["config"]
    assert config.task_type == "RETRIEVAL_DOCUMENT"
    assert config.output_dimensionality == 768
    assert mock_call.call_args.kwargs["model"] == "gemini-embedding-001"
    assert mock_call.call_args.kwargs["contents"] == "hello"


@pytest.mark.asyncio
async def test_embed_query_uses_retrieval_query_task_type() -> None:
    embedder = _make_embedder()
    mock_call = AsyncMock(return_value=_make_embed_response([[0.2] * 768]))
    embedder._client.aio.models.embed_content = mock_call

    result = await embedder.embed_query("query")

    assert result == [0.2] * 768
    config = mock_call.call_args.kwargs["config"]
    assert config.task_type == "RETRIEVAL_QUERY"


@pytest.mark.asyncio
async def test_embed_documents_batches_with_retrieval_document() -> None:
    embedder = _make_embedder()
    vectors = [[0.1] * 768, [0.2] * 768]
    mock_call = AsyncMock(return_value=_make_embed_response(vectors))
    embedder._client.aio.models.embed_content = mock_call

    result = await embedder.embed_documents(["a", "b"])

    assert result == vectors
    config = mock_call.call_args.kwargs["config"]
    assert config.task_type == "RETRIEVAL_DOCUMENT"
    assert mock_call.call_args.kwargs["contents"] == ["a", "b"]


# ---------------------------------------------------------------------------
# C. レスポンス検証
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_document_raises_provider_error_when_embeddings_empty() -> None:
    embedder = _make_embedder()
    response = MagicMock()
    response.embeddings = []
    embedder._client.aio.models.embed_content = AsyncMock(return_value=response)

    with pytest.raises(ProviderError, match="no embeddings"):
        await embedder.embed_document("text")


@pytest.mark.asyncio
async def test_embed_document_raises_provider_error_when_values_missing() -> None:
    embedder = _make_embedder()
    response = MagicMock()
    response.embeddings = [MagicMock(values=None)]
    embedder._client.aio.models.embed_content = AsyncMock(return_value=response)

    with pytest.raises(ProviderError, match="without values"):
        await embedder.embed_document("text")


# ---------------------------------------------------------------------------
# D. _translate_error の分類
# ---------------------------------------------------------------------------


def _api_error(code: int, status: str, message: str = "msg") -> APIError:
    return APIError(code, {"status": status, "message": message})


def _server_error(code: int, status: str, message: str = "msg") -> ServerError:
    return ServerError(code, {"status": status, "message": message})


def test_translate_unauthenticated_to_configuration_error() -> None:
    embedder = _make_embedder()
    result = embedder._translate_error(_api_error(401, "UNAUTHENTICATED"))
    assert isinstance(result, ConfigurationError)


def test_translate_permission_denied_to_configuration_error() -> None:
    embedder = _make_embedder()
    result = embedder._translate_error(_api_error(403, "PERMISSION_DENIED"))
    assert isinstance(result, ConfigurationError)


def test_translate_leaked_key_to_configuration_error() -> None:
    embedder = _make_embedder()
    result = embedder._translate_error(
        _api_error(400, "INVALID_ARGUMENT", "API key reported as leaked")
    )
    assert isinstance(result, ConfigurationError)


def test_translate_invalid_argument_to_invalid_input_error() -> None:
    embedder = _make_embedder()
    result = embedder._translate_error(_api_error(400, "INVALID_ARGUMENT"))
    assert isinstance(result, InvalidInputError)


def test_translate_resource_exhausted_to_rate_limit_error() -> None:
    embedder = _make_embedder()
    result = embedder._translate_error(_api_error(429, "RESOURCE_EXHAUSTED"))
    assert isinstance(result, RateLimitError)


def test_translate_server_error_to_provider_error() -> None:
    embedder = _make_embedder()
    result = embedder._translate_error(_server_error(500, "INTERNAL"))
    assert isinstance(result, ProviderError)


def test_translate_unhandled_api_status_to_unclassified() -> None:
    embedder = _make_embedder()
    result = embedder._translate_error(_api_error(418, "TEAPOT"))
    assert isinstance(result, UnclassifiedError)


def test_translate_timeout_to_network_error() -> None:
    embedder = _make_embedder()
    result = embedder._translate_error(TimeoutError("deadline"))
    assert isinstance(result, NetworkError)


def test_translate_connection_error_to_network_error() -> None:
    embedder = _make_embedder()
    result = embedder._translate_error(ConnectionError("refused"))
    assert isinstance(result, NetworkError)


def test_translate_unknown_to_unclassified() -> None:
    embedder = _make_embedder()
    result = embedder._translate_error(RuntimeError("unexpected"))
    assert isinstance(result, UnclassifiedError)


# ---------------------------------------------------------------------------
# E. SDK 例外伝播経路 (embed_document → _translate_error)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_document_translates_rate_limit_error() -> None:
    embedder = _make_embedder()
    embedder._client.aio.models.embed_content = AsyncMock(
        side_effect=_api_error(429, "RESOURCE_EXHAUSTED")
    )

    with pytest.raises(RateLimitError):
        await embedder.embed_document("text")
