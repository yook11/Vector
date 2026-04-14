"""Tests for content tasks (fetch_content)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.news_article import NewsArticle
from app.services.content_extractor import PermanentFetchError, TemporaryFetchError


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
# fetch_content
# ---------------------------------------------------------------------------


class TestFetchContent:
    @pytest.mark.asyncio
    async def test_idempotency_guard_chains_analyze(self) -> None:
        """Already-fetched article should chain to analyze_article."""
        from app.tasks.content_tasks import fetch_content

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()

        article = _make_article(original_content="already fetched")
        mock_session.get = AsyncMock(return_value=article)

        with (
            patch(
                "app.tasks.content_tasks.SQLModelAsyncSession",
                return_value=_mock_session_context(mock_session),
            ),
            patch("app.tasks.analysis_tasks.analyze_article") as mock_analyze,
        ):
            mock_analyze.kiq = AsyncMock()
            await fetch_content(article_id=1, ctx=mock_ctx)

        mock_session.commit.assert_not_called()
        mock_analyze.kiq.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_permanent_error_sets_skip(self) -> None:
        from app.tasks.content_tasks import fetch_content

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()

        article = _make_article()
        mock_session.get = AsyncMock(return_value=article)

        with (
            patch(
                "app.tasks.content_tasks.SQLModelAsyncSession",
                return_value=_mock_session_context(mock_session),
            ),
            patch(
                "app.tasks.content_tasks.extract_content",
                new_callable=AsyncMock,
                side_effect=PermanentFetchError("HTTP 403"),
            ),
            patch("app.tasks.content_tasks.httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_client
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            await fetch_content(article_id=1, ctx=mock_ctx)

        assert article.skip_content_fetch is True
        mock_session.commit.assert_called()

    @pytest.mark.asyncio
    async def test_temporary_error_raises_for_retry(self) -> None:
        from app.tasks.content_tasks import fetch_content

        mock_session = AsyncMock()
        mock_ctx = _make_ctx(retry_count=0, max_retries=3)

        article = _make_article()
        mock_session.get = AsyncMock(return_value=article)

        with (
            patch(
                "app.tasks.content_tasks.SQLModelAsyncSession",
                return_value=_mock_session_context(mock_session),
            ),
            patch(
                "app.tasks.content_tasks.extract_content",
                new_callable=AsyncMock,
                side_effect=TemporaryFetchError("HTTP 500"),
            ),
            patch("app.tasks.content_tasks.httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_client
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            with pytest.raises(TemporaryFetchError):
                await fetch_content(article_id=1, ctx=mock_ctx)

    @pytest.mark.asyncio
    async def test_temporary_error_last_attempt_sets_skip(self) -> None:
        from app.tasks.content_tasks import fetch_content

        mock_session = AsyncMock()
        mock_ctx = _make_ctx(retry_count=3, max_retries=3)

        article = _make_article()
        mock_session.get = AsyncMock(return_value=article)

        with (
            patch(
                "app.tasks.content_tasks.SQLModelAsyncSession",
                return_value=_mock_session_context(mock_session),
            ),
            patch(
                "app.tasks.content_tasks.extract_content",
                new_callable=AsyncMock,
                side_effect=TemporaryFetchError("HTTP 500"),
            ),
            patch("app.tasks.content_tasks.httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_client
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            await fetch_content(article_id=1, ctx=mock_ctx)

        assert article.skip_content_fetch is True
        mock_session.commit.assert_called()

    @pytest.mark.asyncio
    async def test_quality_gate_none_sets_skip(self) -> None:
        from app.tasks.content_tasks import fetch_content

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()

        article = _make_article()
        mock_session.get = AsyncMock(return_value=article)

        with (
            patch(
                "app.tasks.content_tasks.SQLModelAsyncSession",
                return_value=_mock_session_context(mock_session),
            ),
            patch(
                "app.tasks.content_tasks.extract_content",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("app.tasks.content_tasks.httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_client
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            await fetch_content(article_id=1, ctx=mock_ctx)

        assert article.skip_content_fetch is True

    @pytest.mark.asyncio
    async def test_success_chains_analyze(self) -> None:
        from app.tasks.content_tasks import fetch_content

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()

        article = _make_article()
        mock_session.get = AsyncMock(return_value=article)

        with (
            patch(
                "app.tasks.content_tasks.SQLModelAsyncSession",
                return_value=_mock_session_context(mock_session),
            ),
            patch(
                "app.tasks.content_tasks.extract_content",
                new_callable=AsyncMock,
                return_value="Full article content here.",
            ),
            patch("app.tasks.content_tasks.httpx.AsyncClient") as mock_client_cls,
            patch("app.tasks.analysis_tasks.analyze_article") as mock_analyze,
        ):
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_client
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_analyze.kiq = AsyncMock()
            await fetch_content(article_id=1, ctx=mock_ctx)

        assert article.original_content == "Full article content here."
        mock_analyze.kiq.assert_called_once_with(1)
