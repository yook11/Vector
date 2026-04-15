"""Tests for the embedding service and similar articles API endpoint."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr

from app.analysis import (
    AnalysisDomainError,
    BaseEmbedder,
    InvalidInputError,
    ProviderError,
    RateLimitError,
    get_embedder,
)

# ---------------------------------------------------------------------------
# A. Factory and configuration
# ---------------------------------------------------------------------------


def test_get_embedder_returns_gemini() -> None:
    with patch("app.config.settings") as mock_settings:
        mock_settings.ai_provider = "gemini"
        mock_settings.gemini_api_key = SecretStr("test-key")
        with patch("app.analysis.embedder.gemini.GeminiEmbedder") as MockEmbedder:
            mock_instance = MagicMock(spec=BaseEmbedder)
            MockEmbedder.return_value = mock_instance
            result = get_embedder()
            MockEmbedder.assert_called_once()
            assert result is mock_instance


def test_get_embedder_raises_on_unknown_provider() -> None:
    with patch("app.analysis.embedder.factory.settings") as mock_settings:
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
        patch("app.analysis.embedder.gemini.settings") as mock_settings,
        patch("app.analysis.embedder.gemini.genai"),
    ):
        mock_settings.gemini_api_key = SecretStr("test-key")

        from app.analysis.embedder.gemini import GeminiEmbedder

        embedder = GeminiEmbedder()

        # Create a ClientError with code=429
        error_429 = ClientError(
            429,
            {
                "error": {
                    "message": "RESOURCE_EXHAUSTED",
                    "status": "RESOURCE_EXHAUSTED",
                }
            },
        )

        embedder._client.aio.models.embed_content = AsyncMock(side_effect=error_429)

        with pytest.raises(RateLimitError):
            await embedder.embed_documents(["test text"])


# ---------------------------------------------------------------------------
# E. BaseEmbedder._embed_once (StubEmbedder)
# ---------------------------------------------------------------------------


class _InvalidInputSDKError(Exception):
    """Simulates a provider SDK client error (not an AnalysisDomainError)."""


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

    def _translate_error(self, exc: Exception) -> AnalysisDomainError:
        if isinstance(exc, _InvalidInputSDKError):
            return InvalidInputError(str(exc))
        return ProviderError(str(exc))


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
async def test_embed_once_translates_sdk_error() -> None:
    """SDK exceptions are translated via _translate_error."""
    embedder = StubEmbedder(side_effects=[RuntimeError("API error")])
    with pytest.raises(ProviderError):
        await embedder.embed_document("text")
    assert len(embedder._calls) == 1


@pytest.mark.asyncio
async def test_invalid_input_error_no_retry() -> None:
    embedder = StubEmbedder(side_effects=[_InvalidInputSDKError("bad input")])
    with pytest.raises(InvalidInputError, match="bad input"):
        await embedder.embed_document("text")
    assert len(embedder._calls) == 1


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

            def _translate_error(self, exc: Exception) -> AnalysisDomainError:
                return AnalysisDomainError(str(exc))
