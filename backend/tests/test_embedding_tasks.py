"""Tests for embedding tasks (generate_embedding)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.analysis import AnalysisDomainError
from app.models.article_analysis import ArticleAnalysis
from app.models.news_article import NewsArticle


def _mock_session_context(mock_session: AsyncMock) -> MagicMock:
    """Create a mock async context manager that yields mock_session."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_session)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


def _make_ctx(
    mock_engine: MagicMock | None = None,
    retry_count: int = 0,
    max_retries: int = 0,
) -> MagicMock:
    """Create a mock taskiq Context with state.engine and message labels."""
    ctx = MagicMock()
    ctx.state.engine = mock_engine or MagicMock()
    ctx.message.labels = {
        "retry_count": retry_count,
        "max_retries": max_retries,
    }
    return ctx


def _make_article(
    article_id: int = 1,
    original_content: str | None = None,
    skip_content_fetch: bool = False,
) -> MagicMock:
    """Create a mock NewsArticle."""
    article = MagicMock(spec=NewsArticle)
    article.id = article_id
    article.original_url = f"https://example.com/article-{article_id}"
    article.original_title = f"Article {article_id}"
    article.original_content = original_content
    article.skip_content_fetch = skip_content_fetch
    return article


# ---------------------------------------------------------------------------
# generate_embedding
# ---------------------------------------------------------------------------


class TestGenerateEmbedding:
    @pytest.mark.asyncio
    async def test_idempotency_guard(self) -> None:
        from app.tasks.embedding_tasks import generate_embedding

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()

        analysis = MagicMock(spec=ArticleAnalysis)
        analysis.embedding = [0.1, 0.2]  # already has embedding
        mock_exec = MagicMock()
        mock_exec.scalar_one_or_none.return_value = analysis
        mock_session.execute = AsyncMock(return_value=mock_exec)

        with patch(
            "app.tasks.embedding_tasks.SQLModelAsyncSession",
            return_value=_mock_session_context(mock_session),
        ):
            await generate_embedding(article_id=1, ctx=mock_ctx)

        mock_session.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_embeds_and_commits(self) -> None:
        from app.tasks.embedding_tasks import generate_embedding

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()

        analysis = MagicMock(spec=ArticleAnalysis)
        analysis.embedding = None
        analysis.news_article_id = 1
        mock_exec = MagicMock()
        mock_exec.scalar_one_or_none.return_value = analysis
        mock_session.execute = AsyncMock(return_value=mock_exec)

        article = _make_article(original_content="content")
        mock_session.get = AsyncMock(return_value=article)

        mock_embedder = AsyncMock()
        mock_embedder.MODEL = "gemini-embedding-001"
        mock_embedder.embed_document = AsyncMock(return_value=[0.1] * 768)

        with (
            patch(
                "app.tasks.embedding_tasks.SQLModelAsyncSession",
                return_value=_mock_session_context(mock_session),
            ),
            patch(
                "app.tasks.embedding_tasks.get_embedder",
                return_value=mock_embedder,
            ),
        ):
            await generate_embedding(article_id=1, ctx=mock_ctx)

        assert analysis.embedding == [0.1] * 768
        assert analysis.embedding_model == "gemini-embedding-001"
        mock_session.commit.assert_called()

    @pytest.mark.asyncio
    async def test_embedding_error_raises(self) -> None:
        from app.tasks.embedding_tasks import generate_embedding

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()

        analysis = MagicMock(spec=ArticleAnalysis)
        analysis.embedding = None
        analysis.news_article_id = 1
        mock_exec = MagicMock()
        mock_exec.scalar_one_or_none.return_value = analysis
        mock_session.execute = AsyncMock(return_value=mock_exec)

        article = _make_article(original_content="content")
        mock_session.get = AsyncMock(return_value=article)

        mock_embedder = AsyncMock()
        mock_embedder.embed_document = AsyncMock(
            side_effect=AnalysisDomainError("API down")
        )

        with (
            patch(
                "app.tasks.embedding_tasks.SQLModelAsyncSession",
                return_value=_mock_session_context(mock_session),
            ),
            patch(
                "app.tasks.embedding_tasks.get_embedder",
                return_value=mock_embedder,
            ),
        ):
            with pytest.raises(AnalysisDomainError):
                await generate_embedding(article_id=1, ctx=mock_ctx)
