"""Tests for the embedding service and similar articles API endpoint."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr

from app.ai.embedding import (
    BaseEmbedder,
    EmbeddingError,
    InvalidInputError,
    RateLimitError,
    TransientError,
    get_embedder,
)

# ---------------------------------------------------------------------------
# A. Factory and configuration
# ---------------------------------------------------------------------------


def test_get_embedder_returns_gemini() -> None:
    with patch("app.config.settings") as mock_settings:
        mock_settings.ai_provider = "gemini"
        mock_settings.gemini_api_key = SecretStr("test-key")
        with patch("app.ai.embedding.providers.gemini.GeminiEmbedder") as MockEmbedder:
            mock_instance = MagicMock(spec=BaseEmbedder)
            MockEmbedder.return_value = mock_instance
            result = get_embedder()
            MockEmbedder.assert_called_once()
            assert result is mock_instance


def test_get_embedder_raises_on_unknown_provider() -> None:
    with patch("app.ai.embedding.factory.settings") as mock_settings:
        mock_settings.ai_provider = "unknown_provider"
        with pytest.raises(ValueError, match="Unsupported AI provider"):
            get_embedder()


# ---------------------------------------------------------------------------
# D. gemini_embedder 429 detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gemini_embedder_429_raises_rate_limit_error() -> None:
    """When Gemini API returns 429, GeminiEmbedder should raise RateLimitError."""
    from google.genai.errors import ClientError

    with (
        patch("app.ai.embedding.providers.gemini.settings") as mock_settings,
        patch("app.ai.embedding.providers.gemini.genai"),
        patch("app.ai.embedding.base.asyncio.sleep", AsyncMock()),
    ):
        mock_settings.gemini_api_key = SecretStr("test-key")

        from app.ai.embedding.providers.gemini import GeminiEmbedder

        embedder = GeminiEmbedder()

        # Create a ClientError with code=429
        error_429 = ClientError(429, {"error": {"message": "RESOURCE_EXHAUSTED"}})

        # Always raise 429 to exhaust rate limit retries
        embedder._client.aio.models.embed_content = AsyncMock(side_effect=error_429)

        with pytest.raises(RateLimitError):
            await embedder.embed_documents(["test text"])


# ---------------------------------------------------------------------------
# E. BaseEmbedder._embed_with_retry (StubEmbedder)
# ---------------------------------------------------------------------------


class _RateLimitSDKError(Exception):
    """Simulates a provider SDK rate-limit response (not an EmbeddingError)."""


class _InvalidInputSDKError(Exception):
    """Simulates a provider SDK client error (not an EmbeddingError)."""


class StubEmbedder(BaseEmbedder):
    """Test-only subclass that records _call_api calls and raises on demand.

    _call_api raises plain exceptions (simulating SDK errors).
    _translate_error maps them to the embedding error hierarchy.
    """

    MODEL = "stub-model"
    DIMENSION = 3
    RPM = None
    RPD = None

    def __init__(
        self, *, side_effects: list[list[list[float]] | Exception] | None = None
    ) -> None:
        super().__init__()
        self._side_effects = list(side_effects or [])
        self._calls: list[tuple[str | list[str], str]] = []

    async def _call_api(
        self, contents: str | list[str], task_type: str
    ) -> list[list[float]]:
        self._calls.append((contents, task_type))
        if self._side_effects:
            effect = self._side_effects.pop(0)
            if isinstance(effect, Exception):
                raise effect
            return effect
        return [[0.1, 0.2, 0.3]]

    def _translate_error(self, exc: Exception) -> EmbeddingError:
        if isinstance(exc, _RateLimitSDKError):
            return RateLimitError(str(exc))
        if isinstance(exc, _InvalidInputSDKError):
            return InvalidInputError(str(exc))
        return TransientError(str(exc))


@pytest.mark.asyncio
async def test_embed_document_returns_first_vector() -> None:
    embedder = StubEmbedder(side_effects=[[[1.0, 2.0, 3.0]]])
    result = await embedder.embed_document("hello")
    assert result == [1.0, 2.0, 3.0]


@pytest.mark.asyncio
async def test_embed_documents_returns_all_vectors() -> None:
    vectors = [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
    embedder = StubEmbedder(side_effects=[vectors])
    result = await embedder.embed_documents(["a", "b"])
    assert result == vectors


@pytest.mark.asyncio
async def test_transient_error_retries_with_backoff() -> None:
    embedder = StubEmbedder(
        side_effects=[
            RuntimeError("timeout"),  # attempt 1: transient
            [[0.1, 0.2, 0.3]],  # attempt 2: success
        ]
    )
    with patch("app.ai.embedding.base.asyncio.sleep", AsyncMock()) as mock_sleep:
        result = await embedder.embed_document("text")

    assert result == [0.1, 0.2, 0.3]
    assert len(embedder._calls) == 2
    mock_sleep.assert_called_once_with(2.0)  # first backoff: 2^0 * 2.0


@pytest.mark.asyncio
async def test_transient_error_exhausts_retries() -> None:
    embedder = StubEmbedder(
        side_effects=[
            RuntimeError("err1"),
            RuntimeError("err2"),
            RuntimeError("err3"),
        ]
    )
    with patch("app.ai.embedding.base.asyncio.sleep", AsyncMock()):
        with pytest.raises(EmbeddingError, match="3 attempts"):
            await embedder.embed_document("text")

    assert len(embedder._calls) == 3


@pytest.mark.asyncio
async def test_rate_limit_retries_independently() -> None:
    """Rate limit retry uses fixed delay, doesn't consume normal budget."""
    embedder = StubEmbedder(
        side_effects=[
            _RateLimitSDKError("429"),  # rate limit retry 1
            [[0.1, 0.2, 0.3]],  # success
        ]
    )
    with patch("app.ai.embedding.base.asyncio.sleep", AsyncMock()) as mock_sleep:
        result = await embedder.embed_document("text")

    assert result == [0.1, 0.2, 0.3]
    mock_sleep.assert_called_once_with(10.0)  # fixed RATE_LIMIT_DELAY


@pytest.mark.asyncio
async def test_rate_limit_exhausts_raises() -> None:
    """Exceeding MAX_RATE_LIMIT_RETRIES raises RateLimitError."""
    embedder = StubEmbedder(
        side_effects=[
            _RateLimitSDKError("429 first"),
            _RateLimitSDKError("429 second"),
        ]
    )
    with patch("app.ai.embedding.base.asyncio.sleep", AsyncMock()):
        with pytest.raises(RateLimitError):
            await embedder.embed_document("text")


@pytest.mark.asyncio
async def test_invalid_input_error_no_retry() -> None:
    embedder = StubEmbedder(side_effects=[_InvalidInputSDKError("bad input")])
    with patch("app.ai.embedding.base.asyncio.sleep", AsyncMock()) as mock_sleep:
        with pytest.raises(InvalidInputError, match="bad input"):
            await embedder.embed_document("text")

    assert len(embedder._calls) == 1
    mock_sleep.assert_not_called()


@pytest.mark.asyncio
async def test_task_type_passed_through() -> None:
    embedder = StubEmbedder()
    await embedder.embed_document("doc")
    await embedder.embed_query("query")

    assert embedder._calls[0] == ("doc", "RETRIEVAL_DOCUMENT")
    assert embedder._calls[1] == ("query", "RETRIEVAL_QUERY")


# ---------------------------------------------------------------------------
# F. ClassVar enforcement
# ---------------------------------------------------------------------------


def test_base_embedder_rejects_subclass_without_classvar() -> None:
    """Concrete subclass missing a required ClassVar raises TypeError."""
    with pytest.raises(TypeError, match="must define ClassVar 'RPD'"):

        class BadEmbedder(BaseEmbedder):
            MODEL = "bad"
            DIMENSION = 3
            RPM = None
            # RPD intentionally missing

            async def _call_api(
                self, contents: str | list[str], task_type: str
            ) -> list[list[float]]:
                return [[0.0]]

            def _translate_error(self, exc: Exception) -> EmbeddingError:
                return EmbeddingError(str(exc))
