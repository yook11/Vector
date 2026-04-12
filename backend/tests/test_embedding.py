"""Tests for the embedding service and similar articles API endpoint."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr

from app.services.embedding import (
    BaseEmbedder,
    EmbeddingError,
    EmbedResult,
    InvalidInputError,
    RateLimitError,
    TransientError,
    embed_articles,
    get_embedder,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_analysis(*, has_embedding: bool = False) -> MagicMock:
    """Create a mock ArticleAnalysis with optional embedding."""
    a = MagicMock()
    a.embedding = [0.1] * 768 if has_embedding else None
    a.embedding_model = "text-embedding-004" if has_embedding else None
    a.news_article_id = 1
    return a


def _make_article_mock() -> MagicMock:
    """Create a mock NewsArticle for text building."""
    a = MagicMock()
    a.id = 1
    a.original_title = "Title"
    a.original_content = None
    a.original_description = None
    return a


def _settings_patch():
    """Return a patch context for settings with embed_* fields."""
    return patch(
        "app.services.embedding.settings",
        **{
            "ai_provider": "gemini",
            "embed_batch_size": 20,
            "embed_batch_interval": 8.0,
            "embed_rate_limit_delay": 60.0,
            "embed_max_consecutive_failures": 3,
        },
    )


# ---------------------------------------------------------------------------
# A. Factory and configuration
# ---------------------------------------------------------------------------


def test_get_embedder_returns_gemini() -> None:
    with patch("app.config.settings") as mock_settings:
        mock_settings.ai_provider = "gemini"
        mock_settings.gemini_api_key = SecretStr("test-key")
        with patch("app.services.gemini_embedder.GeminiEmbedder") as MockEmbedder:
            mock_instance = MagicMock(spec=BaseEmbedder)
            MockEmbedder.return_value = mock_instance
            result = get_embedder()
            MockEmbedder.assert_called_once()
            assert result is mock_instance


def test_get_embedder_raises_on_unknown_provider() -> None:
    with patch("app.services.embedding.settings") as mock_settings:
        mock_settings.ai_provider = "unknown_provider"
        with pytest.raises(ValueError, match="Unsupported AI provider"):
            get_embedder()


# ---------------------------------------------------------------------------
# B. embed_articles batch function
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_articles_empty_input_returns_zero_result() -> None:
    mock_session = AsyncMock()
    mock_embedder = AsyncMock(spec=BaseEmbedder)

    result = await embed_articles(mock_session, [], embedder=mock_embedder)

    assert isinstance(result, EmbedResult)
    assert result.embedded_count == 0
    assert result.skipped_count == 0
    assert result.error_count == 0
    mock_embedder.embed_documents.assert_not_called()


@pytest.mark.asyncio
async def test_embed_articles_skips_already_embedded() -> None:
    mock_session = AsyncMock()
    mock_embedder = AsyncMock(spec=BaseEmbedder)

    # Analysis with embedding already set
    analysis = MagicMock()
    analysis.embedding = [0.1, 0.2, 0.3]

    result = await embed_articles(mock_session, [analysis], embedder=mock_embedder)

    assert result.embedded_count == 0
    assert result.skipped_count == 1
    assert result.error_count == 0
    mock_embedder.embed_documents.assert_not_called()


@pytest.mark.asyncio
async def test_embed_articles_success() -> None:
    mock_session = AsyncMock()
    mock_embedder = AsyncMock(spec=BaseEmbedder)

    analysis = MagicMock()
    analysis.embedding = None
    analysis.news_article_id = 1

    # Mock article for text building
    article = MagicMock()
    article.id = 1
    article.original_title = "Quantum Computing Breakthrough"
    article.original_content = "Scientists discovered a new qubit approach."
    article.original_description = None

    # Mock session.execute to return the article
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [article]
    mock_session.execute = AsyncMock(return_value=mock_result)

    mock_embedder.embed_documents = AsyncMock(return_value=[[0.1] * 768])

    result = await embed_articles(mock_session, [analysis], embedder=mock_embedder)

    assert result.embedded_count == 1
    assert result.skipped_count == 0
    assert result.error_count == 0
    mock_embedder.embed_documents.assert_called_once()
    mock_session.commit.assert_called_once()
    # Verify embedding was set on analysis, not article
    assert analysis.embedding == [0.1] * 768
    assert analysis.embedding_model == "text-embedding-004"


@pytest.mark.asyncio
async def test_embed_articles_batch_error_does_not_abort_other_batches() -> None:
    """One batch failure increments error_count but does not abort remaining batches."""
    mock_session = AsyncMock()
    mock_embedder = AsyncMock(spec=BaseEmbedder)

    # Create 25 analyses without embeddings (spans 2 batches of batch_size=20)
    analyses = []
    articles = []
    for i in range(25):
        a = _make_analysis()
        a.news_article_id = i + 1
        analyses.append(a)
        art = _make_article_mock()
        art.id = i + 1
        articles.append(art)

    # Mock session.execute to return articles
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = articles
    mock_session.execute = AsyncMock(return_value=mock_result)

    # First batch succeeds, second batch fails
    mock_embedder.embed_documents = AsyncMock(
        side_effect=[
            [[0.1] * 768] * 20,  # first batch: 20 analyses succeed
            EmbeddingError("API timeout"),  # second batch: 5 analyses fail
        ]
    )

    with (
        patch("app.services.embedding.settings") as mock_settings,
        patch("app.services.embedding.asyncio.sleep", AsyncMock()),
    ):
        mock_settings.embed_batch_size = 20
        mock_settings.embed_batch_interval = 8.0
        mock_settings.embed_rate_limit_delay = 60.0
        mock_settings.embed_max_consecutive_failures = 3
        result = await embed_articles(mock_session, analyses, embedder=mock_embedder)

    assert result.embedded_count == 20
    assert result.error_count == 5
    assert len(result.errors) == 1
    assert "API timeout" in result.errors[0]
    # Both batches were attempted
    assert mock_embedder.embed_documents.call_count == 2


@pytest.mark.asyncio
async def test_embed_articles_uses_description_fallback_when_no_content() -> None:
    """Uses original_description as fallback when original_content is None."""
    mock_session = AsyncMock()
    mock_embedder = AsyncMock(spec=BaseEmbedder)

    analysis = MagicMock()
    analysis.embedding = None
    analysis.news_article_id = 1

    # Mock article for text building — no content, has description
    article = MagicMock()
    article.id = 1
    article.original_title = "AI News"
    article.original_content = None
    article.original_description = "A brief description."

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [article]
    mock_session.execute = AsyncMock(return_value=mock_result)

    mock_embedder.embed_documents = AsyncMock(return_value=[[0.5] * 768])

    await embed_articles(mock_session, [analysis], embedder=mock_embedder)

    # The text passed to embed_batch should include the description
    call_args = mock_embedder.embed_documents.call_args[0][0]  # list of texts
    assert "A brief description." in call_args[0]
    assert "AI News" in call_args[0]


# ---------------------------------------------------------------------------
# C. Rate limit handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_articles_rate_limit_stops_immediately() -> None:
    """RateLimitError should stop processing immediately (daily quota exhausted).

    Only the first batch is attempted; remaining batches are skipped.
    """
    mock_session = AsyncMock()
    mock_embedder = AsyncMock(spec=BaseEmbedder)

    # 4 batches of 10 analyses each
    analyses = []
    articles = []
    for i in range(40):
        a = _make_analysis()
        a.news_article_id = i + 1
        analyses.append(a)
        art = _make_article_mock()
        art.id = i + 1
        articles.append(art)

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = articles
    mock_session.execute = AsyncMock(return_value=mock_result)

    mock_embedder.embed_documents = AsyncMock(
        side_effect=RateLimitError("rate limited"),
    )

    with (
        patch("app.services.embedding.settings") as mock_settings,
        patch("app.services.embedding.asyncio.sleep", AsyncMock()),
    ):
        mock_settings.embed_batch_size = 10
        mock_settings.embed_batch_interval = 8.0
        mock_settings.embed_rate_limit_delay = 60.0
        mock_settings.embed_max_consecutive_failures = 3
        result = await embed_articles(mock_session, analyses, embedder=mock_embedder)

    # Only the first batch was attempted before stopping
    assert mock_embedder.embed_documents.call_count == 1
    assert result.error_count == 10
    assert result.embedded_count == 0


# ---------------------------------------------------------------------------
# D. gemini_embedder 429 detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gemini_embedder_429_raises_rate_limit_error() -> None:
    """When Gemini API returns 429, GeminiEmbedder should raise RateLimitError."""
    from google.genai.errors import ClientError

    with (
        patch("app.services.gemini_embedder.settings") as mock_settings,
        patch("app.services.gemini_embedder.genai"),
        patch("app.services.embedding.asyncio.sleep", AsyncMock()),
    ):
        mock_settings.gemini_api_key = SecretStr("test-key")

        from app.services.gemini_embedder import GeminiEmbedder

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

    def __init__(
        self, *, side_effects: list[list[list[float]] | Exception] | None = None
    ) -> None:
        super().__init__(dimension=3, provider_name="stub")
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
    with patch("app.services.embedding.asyncio.sleep", AsyncMock()) as mock_sleep:
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
    with patch("app.services.embedding.asyncio.sleep", AsyncMock()):
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
    with patch("app.services.embedding.asyncio.sleep", AsyncMock()) as mock_sleep:
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
    with patch("app.services.embedding.asyncio.sleep", AsyncMock()):
        with pytest.raises(RateLimitError):
            await embedder.embed_document("text")


@pytest.mark.asyncio
async def test_invalid_input_error_no_retry() -> None:
    embedder = StubEmbedder(side_effects=[_InvalidInputSDKError("bad input")])
    with patch("app.services.embedding.asyncio.sleep", AsyncMock()) as mock_sleep:
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

