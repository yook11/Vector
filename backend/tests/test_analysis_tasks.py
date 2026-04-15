"""Tests for analysis tasks (analyze_article)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.analysis import AnalysisDomainError, RateLimitError
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
# analyze_article
# ---------------------------------------------------------------------------


class TestAnalyzeArticle:
    @pytest.mark.asyncio
    async def test_idempotency_guard_chains_embedding(self) -> None:
        from app.tasks.analysis_tasks import analyze_article

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()

        existing_analysis = MagicMock(spec=ArticleAnalysis)
        mock_exec = MagicMock()
        mock_exec.scalar_one_or_none.return_value = existing_analysis
        mock_session.execute = AsyncMock(return_value=mock_exec)

        with (
            patch(
                "app.tasks.analysis_tasks.SQLModelAsyncSession",
                return_value=_mock_session_context(mock_session),
            ),
            patch("app.tasks.analysis_tasks.generate_embedding") as mock_embed,
        ):
            mock_embed.kiq = AsyncMock()
            await analyze_article(article_id=1, ctx=mock_ctx)

        mock_session.commit.assert_not_called()
        mock_embed.kiq.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_safety_block_marks_article(self) -> None:
        from app.tasks.analysis_tasks import analyze_article

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()

        mock_exec = MagicMock()
        mock_exec.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_exec)

        article = _make_article(original_content="some content")
        mock_session.get = AsyncMock(return_value=article)

        mock_analyzer = MagicMock()
        mock_analyzer.MODEL = "gemini-2.5-flash-lite"
        mock_analyzer.RPM = 50
        mock_analyzer.RPD = 1500

        with (
            patch(
                "app.tasks.analysis_tasks.SQLModelAsyncSession",
                return_value=_mock_session_context(mock_session),
            ),
            patch(
                "app.tasks.analysis_tasks.get_analyzer",
                return_value=mock_analyzer,
            ),
            patch(
                "app.tasks.analysis_tasks._build_limiters",
                return_value=(None, None),
            ),
            patch(
                "app.tasks.analysis_tasks._analyze_article_svc",
                new_callable=AsyncMock,
                side_effect=AnalysisDomainError("Safety block"),
            ),
        ):
            await analyze_article(article_id=1, ctx=mock_ctx)

        assert article.original_content is None
        assert article.skip_content_fetch is True

    @pytest.mark.asyncio
    async def test_rate_limit_raises_for_retry(self) -> None:
        from app.tasks.analysis_tasks import analyze_article

        mock_session = AsyncMock()
        mock_ctx = _make_ctx(retry_count=0, max_retries=2)

        mock_exec = MagicMock()
        mock_exec.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_exec)

        article = _make_article(original_content="some content")
        mock_session.get = AsyncMock(return_value=article)

        mock_analyzer = MagicMock()
        mock_analyzer.MODEL = "gemini-2.5-flash-lite"
        mock_analyzer.RPM = 50
        mock_analyzer.RPD = 1500

        with (
            patch(
                "app.tasks.analysis_tasks.SQLModelAsyncSession",
                return_value=_mock_session_context(mock_session),
            ),
            patch(
                "app.tasks.analysis_tasks.get_analyzer",
                return_value=mock_analyzer,
            ),
            patch(
                "app.tasks.analysis_tasks._build_limiters",
                return_value=(None, None),
            ),
            patch(
                "app.tasks.analysis_tasks._analyze_article_svc",
                new_callable=AsyncMock,
                side_effect=RateLimitError("429"),
            ),
        ):
            with pytest.raises(RateLimitError):
                await analyze_article(article_id=1, ctx=mock_ctx)

    @pytest.mark.asyncio
    async def test_success_chains_embedding(self) -> None:
        from app.tasks.analysis_tasks import analyze_article

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()

        mock_exec = MagicMock()
        mock_exec.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_exec)

        article = _make_article(original_content="some content")
        mock_session.get = AsyncMock(return_value=article)

        analysis = MagicMock(spec=ArticleAnalysis)

        mock_analyzer = MagicMock()
        mock_analyzer.MODEL = "gemini-2.5-flash-lite"
        mock_analyzer.RPM = 50
        mock_analyzer.RPD = 1500

        with (
            patch(
                "app.tasks.analysis_tasks.SQLModelAsyncSession",
                return_value=_mock_session_context(mock_session),
            ),
            patch(
                "app.tasks.analysis_tasks.get_analyzer",
                return_value=mock_analyzer,
            ),
            patch(
                "app.tasks.analysis_tasks._build_limiters",
                return_value=(None, None),
            ),
            patch(
                "app.tasks.analysis_tasks._analyze_article_svc",
                new_callable=AsyncMock,
                return_value=analysis,
            ),
            patch("app.tasks.analysis_tasks.generate_embedding") as mock_embed,
        ):
            mock_embed.kiq = AsyncMock()
            await analyze_article(article_id=1, ctx=mock_ctx)

        mock_embed.kiq.assert_called_once_with(1)
