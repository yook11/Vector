"""Tests for metadata tasks (fetch_metadata, dispatch_pending)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.news_source import NewsSource
from app.services.news_fetcher import FetchResult, SourceFetchResult


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
    async def test_fetches_and_dispatches(self) -> None:
        from app.tasks.metadata_tasks import fetch_metadata

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()

        source = MagicMock(spec=NewsSource)
        source.id = 1
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [source]
        mock_session.execute = AsyncMock(return_value=mock_result)

        fetch_result = FetchResult(
            new_count=5,
            skipped_count=2,
            error_count=0,
            source_results=[
                SourceFetchResult(
                    source_id=1,
                    success=True,
                    new_count=5,
                    skipped_count=2,
                )
            ],
        )

        with (
            patch(
                "app.tasks.metadata_tasks.SQLModelAsyncSession",
                return_value=_mock_session_context(mock_session),
            ),
            patch(
                "app.tasks.metadata_tasks.fetch_news_for_sources",
                new_callable=AsyncMock,
                return_value=fetch_result,
            ) as mock_fetch,
            patch(
                "app.tasks.metadata_tasks.dispatch_pending",
            ) as mock_dispatch,
        ):
            mock_dispatch.kiq = AsyncMock()
            result = await fetch_metadata(ctx=mock_ctx)

        assert result["sources_count"] == 1
        assert result["fetch_new"] == 5
        mock_fetch.assert_called_once()
        mock_dispatch.kiq.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_when_no_sources(self) -> None:
        from app.tasks.metadata_tasks import fetch_metadata

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        with patch(
            "app.tasks.metadata_tasks.SQLModelAsyncSession",
            return_value=_mock_session_context(mock_session),
        ):
            result = await fetch_metadata(ctx=mock_ctx)

        assert result["sources_count"] == 0


# ---------------------------------------------------------------------------
# dispatch_pending
# ---------------------------------------------------------------------------


class TestDispatchPending:
    @pytest.mark.asyncio
    async def test_dispatches_all_three_queries(self) -> None:
        from app.tasks.metadata_tasks import dispatch_pending

        mock_session = AsyncMock()
        mock_ctx = _make_ctx()

        # Q1: 2 need content, Q2: 1 needs analysis, Q3: 1 needs embedding
        q1 = MagicMock()
        q1.scalars.return_value.all.return_value = [10, 11]
        q2 = MagicMock()
        q2.scalars.return_value.all.return_value = [20]
        q3 = MagicMock()
        q3.scalars.return_value.all.return_value = [30]
        mock_session.execute = AsyncMock(side_effect=[q1, q2, q3])

        with (
            patch(
                "app.tasks.metadata_tasks.SQLModelAsyncSession",
                return_value=_mock_session_context(mock_session),
            ),
            patch("app.tasks.content_tasks.fetch_content") as mock_fc,
            patch("app.tasks.analysis_tasks.analyze_article") as mock_aa,
            patch("app.tasks.embedding_tasks.generate_embedding") as mock_ge,
        ):
            mock_fc.kiq = AsyncMock()
            mock_aa.kiq = AsyncMock()
            mock_ge.kiq = AsyncMock()

            result = await dispatch_pending(ctx=mock_ctx)

        assert result["fetch_content"] == 2
        assert result["analyze_article"] == 1
        assert result["generate_embedding"] == 1
        assert mock_fc.kiq.call_count == 2
        assert mock_aa.kiq.call_count == 1
        assert mock_ge.kiq.call_count == 1
