"""Tests for the scheduler service."""

from unittest.mock import AsyncMock, MagicMock, patch

from app.models.keyword import Keyword
from app.models.news import NewsArticle
from app.services.ai_analyzer import AnalyzeResult
from app.services.news_fetcher import FetchResult
from app.services.scheduler import (
    SchedulerJobResult,
    run_fetch_and_analyze,
    start_scheduler,
    stop_scheduler,
)


def _mock_session_context(mock_session: AsyncMock) -> MagicMock:
    """Create a mock context manager that yields mock_session."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_session)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


# --- A. Job function unit tests ---


async def test_job_fetches_and_analyzes_successfully() -> None:
    mock_session = AsyncMock()

    kw = Keyword(id=1, keyword="Quantum", category="test", is_active=True)
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
            "app.services.scheduler.SQLModelAsyncSession",
            return_value=_mock_session_context(mock_session),
        ),
        patch(
            "app.services.scheduler.fetch_news_for_keywords",
            new_callable=AsyncMock,
            return_value=FetchResult(new_count=5, skipped_count=2, error_count=0),
        ) as mock_fetch,
        patch(
            "app.services.scheduler.analyze_articles",
            new_callable=AsyncMock,
            return_value=AnalyzeResult(
                analyzed_count=3, skipped_count=1, error_count=0
            ),
        ) as mock_analyze,
    ):
        result = await run_fetch_and_analyze()

    assert result.keywords_count == 1
    assert result.fetch_new == 5
    assert result.fetch_skipped == 2
    assert result.fetch_errors == 0
    assert result.analyze_count == 3
    assert result.analyze_skipped == 1
    assert result.analyze_errors == 0
    mock_fetch.assert_called_once()
    mock_analyze.assert_called_once()


async def test_job_skips_when_no_active_keywords() -> None:
    mock_session = AsyncMock()

    mock_kw_result = MagicMock()
    mock_kw_result.scalars.return_value.all.return_value = []

    mock_session.execute = AsyncMock(return_value=mock_kw_result)

    with (
        patch(
            "app.services.scheduler.SQLModelAsyncSession",
            return_value=_mock_session_context(mock_session),
        ),
        patch(
            "app.services.scheduler.fetch_news_for_keywords",
            new_callable=AsyncMock,
        ) as mock_fetch,
        patch(
            "app.services.scheduler.analyze_articles",
            new_callable=AsyncMock,
        ) as mock_analyze,
    ):
        result = await run_fetch_and_analyze()

    assert result.keywords_count == 0
    assert result.fetch_new == 0
    assert result.analyze_count == 0
    mock_fetch.assert_not_called()
    mock_analyze.assert_not_called()


async def test_job_skips_analysis_when_no_unanalyzed_articles() -> None:
    mock_session = AsyncMock()

    kw = Keyword(id=1, keyword="AI", category="test", is_active=True)
    mock_kw_result = MagicMock()
    mock_kw_result.scalars.return_value.all.return_value = [kw]

    mock_article_result = MagicMock()
    mock_article_result.scalars.return_value.all.return_value = []

    mock_session.execute = AsyncMock(side_effect=[mock_kw_result, mock_article_result])

    with (
        patch(
            "app.services.scheduler.SQLModelAsyncSession",
            return_value=_mock_session_context(mock_session),
        ),
        patch(
            "app.services.scheduler.fetch_news_for_keywords",
            new_callable=AsyncMock,
            return_value=FetchResult(new_count=0, skipped_count=3, error_count=0),
        ),
        patch(
            "app.services.scheduler.analyze_articles",
            new_callable=AsyncMock,
        ) as mock_analyze,
    ):
        result = await run_fetch_and_analyze()

    assert result.keywords_count == 1
    assert result.fetch_skipped == 3
    assert result.analyze_count == 0
    mock_analyze.assert_not_called()


async def test_job_handles_database_error_gracefully() -> None:
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(side_effect=Exception("connection refused"))
    ctx.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "app.services.scheduler.SQLModelAsyncSession",
        return_value=ctx,
    ):
        result = await run_fetch_and_analyze()

    assert result == SchedulerJobResult()


async def test_job_handles_fetch_error_gracefully() -> None:
    mock_session = AsyncMock()

    kw = Keyword(id=1, keyword="Test", category="test", is_active=True)
    mock_kw_result = MagicMock()
    mock_kw_result.scalars.return_value.all.return_value = [kw]

    mock_session.execute = AsyncMock(return_value=mock_kw_result)

    with (
        patch(
            "app.services.scheduler.SQLModelAsyncSession",
            return_value=_mock_session_context(mock_session),
        ),
        patch(
            "app.services.scheduler.fetch_news_for_keywords",
            new_callable=AsyncMock,
            side_effect=Exception("unexpected fetch error"),
        ),
    ):
        result = await run_fetch_and_analyze()

    assert result.keywords_count == 1
    assert result.fetch_new == 0


async def test_job_reports_analysis_errors() -> None:
    mock_session = AsyncMock()

    kw = Keyword(id=1, keyword="Materials", category="test", is_active=True)
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
            "app.services.scheduler.SQLModelAsyncSession",
            return_value=_mock_session_context(mock_session),
        ),
        patch(
            "app.services.scheduler.fetch_news_for_keywords",
            new_callable=AsyncMock,
            return_value=FetchResult(new_count=3, skipped_count=0, error_count=0),
        ),
        patch(
            "app.services.scheduler.analyze_articles",
            new_callable=AsyncMock,
            return_value=AnalyzeResult(
                analyzed_count=1, skipped_count=0, error_count=2
            ),
        ),
    ):
        result = await run_fetch_and_analyze()

    assert result.analyze_count == 1
    assert result.analyze_errors == 2


# --- B. Scheduler lifecycle tests ---


def test_start_scheduler_creates_and_starts() -> None:
    with patch("app.services.scheduler.AsyncIOScheduler") as MockScheduler:
        mock_instance = MagicMock()
        MockScheduler.return_value = mock_instance

        start_scheduler()

        MockScheduler.assert_called_once()
        mock_instance.add_job.assert_called_once()
        call_kwargs = mock_instance.add_job.call_args
        assert call_kwargs[1]["trigger"] == "interval"
        assert call_kwargs[1]["max_instances"] == 1
        assert call_kwargs[1]["replace_existing"] is True
        mock_instance.start.assert_called_once()

    # Clean up module-level state
    import app.services.scheduler as sched_mod

    sched_mod._scheduler = None


def test_stop_scheduler_shuts_down() -> None:
    import app.services.scheduler as sched_mod

    mock_scheduler = MagicMock()
    mock_scheduler.running = True
    sched_mod._scheduler = mock_scheduler

    stop_scheduler()

    mock_scheduler.shutdown.assert_called_once_with(wait=False)
    assert sched_mod._scheduler is None


def test_stop_scheduler_noop_when_not_started() -> None:
    import app.services.scheduler as sched_mod

    sched_mod._scheduler = None

    stop_scheduler()  # Should not raise
