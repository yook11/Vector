"""Tests for the embedding service and similar articles API endpoint."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.embedding import (
    BaseEmbedder,
    EmbedResult,
    EmbeddingError,
    embed_articles,
    get_embedder,
)


# ---------------------------------------------------------------------------
# A. Factory and configuration
# ---------------------------------------------------------------------------


def test_get_embedder_returns_gemini() -> None:
    with patch("app.config.settings") as mock_settings:
        mock_settings.ai_provider = "gemini"
        mock_settings.gemini_api_key = "test-key"
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
    mock_embedder.embed_batch.assert_not_called()


@pytest.mark.asyncio
async def test_embed_articles_skips_already_embedded() -> None:
    mock_session = AsyncMock()
    mock_embedder = AsyncMock(spec=BaseEmbedder)

    # Article with embedding already set
    article = MagicMock()
    article.embedding = [0.1, 0.2, 0.3]

    result = await embed_articles(mock_session, [article], embedder=mock_embedder)

    assert result.embedded_count == 0
    assert result.skipped_count == 1
    assert result.error_count == 0
    mock_embedder.embed_batch.assert_not_called()


@pytest.mark.asyncio
async def test_embed_articles_success() -> None:
    mock_session = AsyncMock()
    mock_embedder = AsyncMock(spec=BaseEmbedder)

    article = MagicMock()
    article.embedding = None
    article.title_original = "Quantum Computing Breakthrough"
    article.content = "Scientists discovered a new qubit approach."
    article.description_original = None

    mock_embedder.embed_batch = AsyncMock(return_value=[[0.1] * 768])

    result = await embed_articles(mock_session, [article], embedder=mock_embedder)

    assert result.embedded_count == 1
    assert result.skipped_count == 0
    assert result.error_count == 0
    mock_embedder.embed_batch.assert_called_once()
    mock_session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_embed_articles_batch_error_does_not_abort_other_batches() -> None:
    """One batch failure increments error_count but does not abort remaining batches."""
    mock_session = AsyncMock()
    mock_embedder = AsyncMock(spec=BaseEmbedder)

    # Create 25 articles without embeddings (spans 2 batches of BATCH_SIZE=20)
    articles = []
    for _ in range(25):
        a = MagicMock()
        a.embedding = None
        a.title_original = "Title"
        a.content = None
        a.description_original = None
        articles.append(a)

    # First batch succeeds, second batch fails
    mock_embedder.embed_batch = AsyncMock(
        side_effect=[
            [[0.1] * 768] * 20,   # first batch: 20 articles succeed
            EmbeddingError("API timeout"),  # second batch: 5 articles fail
        ]
    )

    with patch("app.services.embedding.asyncio.sleep", AsyncMock()):
        result = await embed_articles(mock_session, articles, embedder=mock_embedder)

    assert result.embedded_count == 20
    assert result.error_count == 5
    assert len(result.errors) == 1
    assert "API timeout" in result.errors[0]
    # Both batches were attempted
    assert mock_embedder.embed_batch.call_count == 2


@pytest.mark.asyncio
async def test_embed_articles_uses_description_fallback_when_no_content() -> None:
    """Uses description_original as fallback when content is None."""
    mock_session = AsyncMock()
    mock_embedder = AsyncMock(spec=BaseEmbedder)

    article = MagicMock()
    article.embedding = None
    article.title_original = "AI News"
    article.content = None
    article.description_original = "A brief description."

    mock_embedder.embed_batch = AsyncMock(return_value=[[0.5] * 768])

    await embed_articles(mock_session, [article], embedder=mock_embedder)

    # The text passed to embed_batch should include the description
    call_args = mock_embedder.embed_batch.call_args[0][0]  # list of texts
    assert "A brief description." in call_args[0]
    assert "AI News" in call_args[0]


# ---------------------------------------------------------------------------
# C. Similar articles API — unit tests (no DB required)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_similar_news_returns_404_for_missing_article() -> None:
    from fastapi.testclient import TestClient
    from app.main import app
    from app.dependencies import get_session

    mock_session = AsyncMock()
    # scalar_one_or_none returns None (article not found)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute = AsyncMock(return_value=mock_result)

    async def override_get_session():
        yield mock_session

    app.dependency_overrides[get_session] = override_get_session
    try:
        client = TestClient(app)
        response = client.get("/api/v1/news/9999/similar")
        assert response.status_code == 404
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_similar_news_returns_empty_list_when_no_embedding() -> None:
    from fastapi.testclient import TestClient
    from app.main import app
    from app.dependencies import get_session

    mock_session = AsyncMock()
    article = MagicMock()
    article.embedding = None  # no embedding yet

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = article
    mock_session.execute = AsyncMock(return_value=mock_result)

    async def override_get_session():
        yield mock_session

    app.dependency_overrides[get_session] = override_get_session
    try:
        client = TestClient(app)
        response = client.get("/api/v1/news/1/similar")
        assert response.status_code == 200
        assert response.json() == []
    finally:
        app.dependency_overrides.clear()
