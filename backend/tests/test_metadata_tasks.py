"""Tests for metadata tasks (fetch_metadata)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.collection.news_fetcher import FetchResult, SourceFetchResult
from app.models.news_source import NewsSource


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


# ---------------------------------------------------------------------------
# fetch_metadata
# ---------------------------------------------------------------------------


class TestFetchMetadata:
    @pytest.mark.asyncio
    async def test_fetches_and_dispatches_content(self) -> None:
        from app.tasks.collection_tasks import fetch_metadata

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()

        source = MagicMock(spec=NewsSource)
        source.id = 1
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [source]
        mock_session.execute = AsyncMock(return_value=mock_result)

        fetch_result = FetchResult(
            new_count=2,
            skipped_count=0,
            error_count=0,
            source_results=[
                SourceFetchResult(source_id=1, success=True, new_count=2),
            ],
            new_article_ids=[10, 11],
            content_ready_ids=[],
        )

        with (
            patch(
                "app.tasks.collection_tasks.SQLModelAsyncSession",
                return_value=_mock_session_context(mock_session),
            ),
            patch(
                "app.tasks.collection_tasks.fetch_news_for_sources",
                new_callable=AsyncMock,
                return_value=fetch_result,
            ) as mock_fetch,
            patch("app.tasks.collection_tasks.fetch_content") as mock_fc,
            patch("app.tasks.analysis_tasks.analyze_article") as mock_aa,
        ):
            mock_fc.kiq = AsyncMock()
            mock_aa.kiq = AsyncMock()
            result = await fetch_metadata(ctx=mock_ctx)

        assert result["sources_count"] == 1
        assert result["fetch_new"] == 2
        mock_fetch.assert_called_once()
        assert mock_fc.kiq.call_count == 2
        mock_aa.kiq.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatches_content_ready_to_analysis(self) -> None:
        """Full-text RSS articles go directly to analyze_article."""
        from app.tasks.collection_tasks import fetch_metadata

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()

        source = MagicMock(spec=NewsSource)
        source.id = 1
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [source]
        mock_session.execute = AsyncMock(return_value=mock_result)

        fetch_result = FetchResult(
            new_count=3,
            skipped_count=0,
            error_count=0,
            source_results=[
                SourceFetchResult(source_id=1, success=True, new_count=3),
            ],
            new_article_ids=[10, 11, 12],
            content_ready_ids=[10, 12],
        )

        with (
            patch(
                "app.tasks.collection_tasks.SQLModelAsyncSession",
                return_value=_mock_session_context(mock_session),
            ),
            patch(
                "app.tasks.collection_tasks.fetch_news_for_sources",
                new_callable=AsyncMock,
                return_value=fetch_result,
            ),
            patch("app.tasks.collection_tasks.fetch_content") as mock_fc,
            patch("app.tasks.analysis_tasks.analyze_article") as mock_aa,
        ):
            mock_fc.kiq = AsyncMock()
            mock_aa.kiq = AsyncMock()
            await fetch_metadata(ctx=mock_ctx)

        # article 11 needs content fetch
        mock_fc.kiq.assert_called_once_with(11)
        # articles 10, 12 have content ready — go to analysis
        assert mock_aa.kiq.call_count == 2

    @pytest.mark.asyncio
    async def test_skips_when_no_sources(self) -> None:
        from app.tasks.collection_tasks import fetch_metadata

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        with patch(
            "app.tasks.collection_tasks.SQLModelAsyncSession",
            return_value=_mock_session_context(mock_session),
        ):
            result = await fetch_metadata(ctx=mock_ctx)

        assert result["sources_count"] == 0
