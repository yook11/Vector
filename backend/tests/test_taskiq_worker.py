"""Tests for the taskiq production worker task function."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.keyword import Keyword
from app.models.news import NewsArticle
from app.services.ai_analyzer import AnalyzeResult
from app.services.content_extractor import ContentExtractionResult
from app.services.news_fetcher import FetchResult
from app.tasks.taskiq_worker import fetch_and_analyze_task


def _mock_session_context(mock_session: AsyncMock) -> MagicMock:
    """Create a mock async context manager that yields mock_session."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_session)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


def _make_ctx(mock_engine: MagicMock | None = None) -> MagicMock:
    """Create a mock taskiq Context with a state.engine."""
    ctx = MagicMock()
    ctx.state.engine = mock_engine or MagicMock()
    return ctx


# ---------------------------------------------------------------------------
# A. Task function unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_fetches_and_analyzes_successfully() -> None:
    mock_session = AsyncMock()
    mock_ctx = _make_ctx()

    kw = Keyword(id=1, keyword="Quantum")
    mock_kw_result = MagicMock()
    mock_kw_result.scalars.return_value.all.return_value = [kw]

    # Phase 3: no articles needing content extraction
    mock_no_content_result = MagicMock()
    mock_no_content_result.scalars.return_value.all.return_value = []

    article = MagicMock(spec=NewsArticle)
    mock_article_result = MagicMock()
    mock_article_result.scalars.return_value.all.return_value = [article]

    mock_session.execute = AsyncMock(
        side_effect=[mock_kw_result, mock_no_content_result, mock_article_result]
    )

    with (
        patch(
            "app.tasks.taskiq_worker.SQLModelAsyncSession",
            return_value=_mock_session_context(mock_session),
        ),
        patch(
            "app.tasks.taskiq_worker.fetch_news_for_keywords",
            new_callable=AsyncMock,
            return_value=FetchResult(new_count=5, skipped_count=2, error_count=0),
        ) as mock_fetch,
        patch(
            "app.tasks.taskiq_worker.extract_contents",
            new_callable=AsyncMock,
        ) as mock_extract,
        patch(
            "app.tasks.taskiq_worker.analyze_articles",
            new_callable=AsyncMock,
            return_value=AnalyzeResult(
                analyzed_count=3, skipped_count=1, error_count=0
            ),
        ) as mock_analyze,
    ):
        result = await fetch_and_analyze_task(ctx=mock_ctx)

    assert result["keywords_count"] == 1
    assert result["fetch_new"] == 5
    assert result["fetch_skipped"] == 2
    assert result["fetch_errors"] == 0
    assert result["content_extracted"] == 0  # skipped (no_content list was empty)
    assert result["analyze_count"] == 3
    assert result["analyze_skipped"] == 1
    assert result["analyze_errors"] == 0
    mock_fetch.assert_called_once()
    mock_extract.assert_not_called()  # skipped because no_content was empty
    mock_analyze.assert_called_once()


@pytest.mark.asyncio
async def test_task_skips_when_no_keywords() -> None:
    mock_session = AsyncMock()
    mock_ctx = _make_ctx()

    mock_kw_result = MagicMock()
    mock_kw_result.scalars.return_value.all.return_value = []
    mock_session.execute = AsyncMock(return_value=mock_kw_result)

    with (
        patch(
            "app.tasks.taskiq_worker.SQLModelAsyncSession",
            return_value=_mock_session_context(mock_session),
        ),
        patch(
            "app.tasks.taskiq_worker.fetch_news_for_keywords",
            new_callable=AsyncMock,
        ) as mock_fetch,
        patch(
            "app.tasks.taskiq_worker.analyze_articles",
            new_callable=AsyncMock,
        ) as mock_analyze,
    ):
        result = await fetch_and_analyze_task(ctx=mock_ctx)

    assert result["keywords_count"] == 0
    assert result["fetch_new"] == 0
    assert result["analyze_count"] == 0
    mock_fetch.assert_not_called()
    mock_analyze.assert_not_called()


@pytest.mark.asyncio
async def test_task_skips_analysis_when_no_unanalyzed_articles() -> None:
    mock_session = AsyncMock()
    mock_ctx = _make_ctx()

    kw = Keyword(id=1, keyword="AI")
    mock_kw_result = MagicMock()
    mock_kw_result.scalars.return_value.all.return_value = [kw]

    # Phase 3: no articles without content
    mock_no_content_result = MagicMock()
    mock_no_content_result.scalars.return_value.all.return_value = []

    # Phase 4: no unanalyzed articles
    mock_unanalyzed_result = MagicMock()
    mock_unanalyzed_result.scalars.return_value.all.return_value = []

    mock_session.execute = AsyncMock(
        side_effect=[mock_kw_result, mock_no_content_result, mock_unanalyzed_result]
    )

    with (
        patch(
            "app.tasks.taskiq_worker.SQLModelAsyncSession",
            return_value=_mock_session_context(mock_session),
        ),
        patch(
            "app.tasks.taskiq_worker.fetch_news_for_keywords",
            new_callable=AsyncMock,
            return_value=FetchResult(new_count=0, skipped_count=3, error_count=0),
        ),
        patch(
            "app.tasks.taskiq_worker.analyze_articles",
            new_callable=AsyncMock,
        ) as mock_analyze,
    ):
        result = await fetch_and_analyze_task(ctx=mock_ctx)

    assert result["keywords_count"] == 1
    assert result["fetch_skipped"] == 3
    assert result["analyze_count"] == 0
    mock_analyze.assert_not_called()


@pytest.mark.asyncio
async def test_task_skips_content_extraction_when_all_have_content() -> None:
    mock_session = AsyncMock()
    mock_ctx = _make_ctx()

    kw = Keyword(id=1, keyword="Materials")
    mock_kw_result = MagicMock()
    mock_kw_result.scalars.return_value.all.return_value = [kw]

    # Phase 3: all articles already have content
    mock_no_content_result = MagicMock()
    mock_no_content_result.scalars.return_value.all.return_value = []

    # Phase 4: no unanalyzed articles either
    mock_unanalyzed_result = MagicMock()
    mock_unanalyzed_result.scalars.return_value.all.return_value = []

    mock_session.execute = AsyncMock(
        side_effect=[mock_kw_result, mock_no_content_result, mock_unanalyzed_result]
    )

    with (
        patch(
            "app.tasks.taskiq_worker.SQLModelAsyncSession",
            return_value=_mock_session_context(mock_session),
        ),
        patch(
            "app.tasks.taskiq_worker.fetch_news_for_keywords",
            new_callable=AsyncMock,
            return_value=FetchResult(new_count=0, skipped_count=5, error_count=0),
        ),
        patch(
            "app.tasks.taskiq_worker.extract_contents",
            new_callable=AsyncMock,
        ) as mock_extract,
    ):
        result = await fetch_and_analyze_task(ctx=mock_ctx)

    assert result["content_extracted"] == 0
    assert result["content_errors"] == 0
    mock_extract.assert_not_called()


@pytest.mark.asyncio
async def test_task_continues_phase3_when_phase2_fails() -> None:
    """Phase 3 runs independently even if Phase 2 raises an exception."""
    mock_session = AsyncMock()
    mock_ctx = _make_ctx()

    kw = Keyword(id=1, keyword="Test")
    mock_kw_result = MagicMock()
    mock_kw_result.scalars.return_value.all.return_value = [kw]

    article = MagicMock(spec=NewsArticle)
    mock_no_content_result = MagicMock()
    mock_no_content_result.scalars.return_value.all.return_value = [article]

    mock_unanalyzed_result = MagicMock()
    mock_unanalyzed_result.scalars.return_value.all.return_value = []

    mock_session.execute = AsyncMock(
        side_effect=[mock_kw_result, mock_no_content_result, mock_unanalyzed_result]
    )

    with (
        patch(
            "app.tasks.taskiq_worker.SQLModelAsyncSession",
            return_value=_mock_session_context(mock_session),
        ),
        patch(
            "app.tasks.taskiq_worker.fetch_news_for_keywords",
            new_callable=AsyncMock,
            side_effect=Exception("RSS feed unreachable"),
        ),
        patch(
            "app.tasks.taskiq_worker.extract_contents",
            new_callable=AsyncMock,
            return_value=ContentExtractionResult(
                extracted_count=2, skipped_count=0, error_count=0
            ),
        ) as mock_extract,
    ):
        result = await fetch_and_analyze_task(ctx=mock_ctx)

    # Phase 2 error is counted, task still completes (not re-raised)
    assert result["fetch_errors"] == 1
    assert result["keywords_count"] == 1
    # Phase 3 ran despite Phase 2 failure
    mock_extract.assert_called_once()
    assert result["content_extracted"] == 2


@pytest.mark.asyncio
async def test_task_reports_analysis_errors() -> None:
    mock_session = AsyncMock()
    mock_ctx = _make_ctx()

    kw = Keyword(id=1, keyword="Materials")
    mock_kw_result = MagicMock()
    mock_kw_result.scalars.return_value.all.return_value = [kw]

    mock_no_content_result = MagicMock()
    mock_no_content_result.scalars.return_value.all.return_value = []

    article = MagicMock(spec=NewsArticle)
    mock_article_result = MagicMock()
    mock_article_result.scalars.return_value.all.return_value = [article]

    mock_session.execute = AsyncMock(
        side_effect=[mock_kw_result, mock_no_content_result, mock_article_result]
    )

    with (
        patch(
            "app.tasks.taskiq_worker.SQLModelAsyncSession",
            return_value=_mock_session_context(mock_session),
        ),
        patch(
            "app.tasks.taskiq_worker.fetch_news_for_keywords",
            new_callable=AsyncMock,
            return_value=FetchResult(new_count=3, skipped_count=0, error_count=0),
        ),
        patch(
            "app.tasks.taskiq_worker.analyze_articles",
            new_callable=AsyncMock,
            return_value=AnalyzeResult(
                analyzed_count=1, skipped_count=0, error_count=2
            ),
        ),
    ):
        result = await fetch_and_analyze_task(ctx=mock_ctx)

    assert result["analyze_count"] == 1
    assert result["analyze_errors"] == 2
