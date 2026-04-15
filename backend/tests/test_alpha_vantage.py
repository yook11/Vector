"""Tests for Alpha Vantage News Sentiment API client."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.collection.alpha_vantage import (
    AlphaVantageClient,
    _parse_av_time,
)
from app.models.news_article import NewsArticle
from app.models.news_source import NewsSource

SAMPLE_AV_RESPONSE = {
    "items": "2",
    "sentiment_score_definition": "...",
    "relevance_score_definition": "...",
    "feed": [
        {
            "title": "AI Breakthrough in Chip Design",
            "url": "https://example.com/article-1",
            "time_published": "20260305T143000",
            "summary": "A new AI model can design chips faster.",
            "source": "TechNews",
        },
        {
            "title": "Quantum Computing Milestone",
            "url": "https://example.com/article-2",
            "time_published": "20260305T120000",
            "summary": "Researchers achieve quantum supremacy.",
            "source": "SciDaily",
        },
    ],
}


def test_parse_av_time_with_seconds() -> None:
    """Parse standard YYYYMMDDTHHMMSS format."""
    result = _parse_av_time("20260305T143000")
    assert result == datetime(2026, 3, 5, 14, 30, 0, tzinfo=UTC)


def test_parse_av_time_without_seconds() -> None:
    """Parse fallback YYYYMMDDTHHMM format."""
    result = _parse_av_time("20260305T1430")
    assert result == datetime(2026, 3, 5, 14, 30, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_av_fetch_skips_when_no_api_key(
    db_session: AsyncSession,
    sample_av_source: NewsSource,
) -> None:
    """When av_api_key is empty, fetch is skipped (not an error)."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)

    with patch("app.collection.alpha_vantage.settings") as mock_settings:
        mock_settings.av_api_key = SecretStr("")
        mock_settings.av_api_base_url = "https://www.alphavantage.co/query"

        client = AlphaVantageClient(mock_http)
        result = await client.fetch_and_save_articles(sample_av_source, db_session)

    assert result.success is True
    assert result.new_count == 0
    mock_http.get.assert_not_called()


@pytest.mark.asyncio
async def test_av_fetch_saves_articles(
    db_session: AsyncSession,
    sample_av_source: NewsSource,
) -> None:
    """Successful AV fetch creates NewsArticle records."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = SAMPLE_AV_RESPONSE
    mock_response.raise_for_status = MagicMock()

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get = AsyncMock(return_value=mock_response)

    with patch("app.collection.alpha_vantage.settings") as mock_settings:
        mock_settings.av_api_key = SecretStr("test-key")
        mock_settings.av_api_base_url = "https://www.alphavantage.co/query"
        mock_settings.av_topics = "technology"
        mock_settings.av_limit = 50
        mock_settings.av_max_daily_requests = 25
        mock_settings.max_articles_per_fetch = 50

        client = AlphaVantageClient(mock_http)
        result = await client.fetch_and_save_articles(sample_av_source, db_session)

    assert result.success is True
    assert result.new_count == 2
    assert result.skipped_count == 0

    # Verify articles in DB
    await db_session.commit()
    stmt = select(NewsArticle).where(NewsArticle.news_source_id == sample_av_source.id)
    rows = await db_session.execute(stmt)
    articles = rows.scalars().all()
    assert len(articles) == 2

    titles = {a.original_title for a in articles}
    assert "AI Breakthrough in Chip Design" in titles
    assert "Quantum Computing Milestone" in titles

    for a in articles:
        assert a.original_url is not None
        assert a.news_source_id == sample_av_source.id


@pytest.mark.asyncio
async def test_av_fetch_skips_duplicates(
    db_session: AsyncSession,
    sample_av_source: NewsSource,
) -> None:
    """Already existing articles are skipped."""
    # Pre-insert one article
    existing_url = "https://example.com/article-1"
    existing = NewsArticle(
        original_title="Existing",
        original_url=existing_url,
        news_source_id=sample_av_source.id,
    )
    db_session.add(existing)
    await db_session.commit()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = SAMPLE_AV_RESPONSE
    mock_response.raise_for_status = MagicMock()

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get = AsyncMock(return_value=mock_response)

    with patch("app.collection.alpha_vantage.settings") as mock_settings:
        mock_settings.av_api_key = SecretStr("test-key")
        mock_settings.av_api_base_url = "https://www.alphavantage.co/query"
        mock_settings.av_topics = "technology"
        mock_settings.av_limit = 50
        mock_settings.av_max_daily_requests = 25
        mock_settings.max_articles_per_fetch = 50

        client = AlphaVantageClient(mock_http)
        result = await client.fetch_and_save_articles(sample_av_source, db_session)

    assert result.new_count == 1
    assert result.skipped_count == 1


@pytest.mark.asyncio
async def test_av_fetch_handles_api_error_response(
    db_session: AsyncSession,
    sample_av_source: NewsSource,
) -> None:
    """AV API returns HTTP 200 with Information field on error."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "Information": "Thank you for using Alpha Vantage! API call frequency exceeded."
    }
    mock_response.raise_for_status = MagicMock()

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get = AsyncMock(return_value=mock_response)

    with patch("app.collection.alpha_vantage.settings") as mock_settings:
        mock_settings.av_api_key = SecretStr("test-key")
        mock_settings.av_api_base_url = "https://www.alphavantage.co/query"
        mock_settings.av_topics = "technology"
        mock_settings.av_limit = 50
        mock_settings.av_max_daily_requests = 25

        client = AlphaVantageClient(mock_http)
        result = await client.fetch_and_save_articles(sample_av_source, db_session)

    assert result.success is False
    assert "API call frequency exceeded" in result.error_message


@pytest.mark.asyncio
async def test_av_fetch_handles_http_error(
    db_session: AsyncSession,
    sample_av_source: NewsSource,
) -> None:
    """HTTP errors are handled gracefully."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "Server Error",
            request=httpx.Request("GET", "https://www.alphavantage.co/query"),
            response=httpx.Response(503),
        )
    )

    with patch("app.collection.alpha_vantage.settings") as mock_settings:
        mock_settings.av_api_key = SecretStr("test-key")
        mock_settings.av_api_base_url = "https://www.alphavantage.co/query"
        mock_settings.av_topics = "technology"
        mock_settings.av_limit = 50
        mock_settings.av_max_daily_requests = 25

        client = AlphaVantageClient(mock_http)
        result = await client.fetch_and_save_articles(sample_av_source, db_session)

    assert result.success is False
    assert result.error_message == "HTTP 503"


@pytest.mark.asyncio
async def test_av_fetch_quota_exceeded(
    db_session: AsyncSession,
    sample_av_source: NewsSource,
) -> None:
    """Fetch is blocked when daily quota is exceeded."""
    from app.models.fetch_log import FetchLog, FetchStatus

    # Insert enough fetch logs to exceed quota
    now = datetime.now(UTC)
    for _ in range(25):
        log = FetchLog(
            source_id=sample_av_source.id,
            status=FetchStatus.SUCCESS,
            articles_count=0,
            fetched_at=now,
        )
        db_session.add(log)
    await db_session.commit()

    mock_http = AsyncMock(spec=httpx.AsyncClient)

    with patch("app.collection.alpha_vantage.settings") as mock_settings:
        mock_settings.av_api_key = SecretStr("test-key")
        mock_settings.av_api_base_url = "https://www.alphavantage.co/query"
        mock_settings.av_max_daily_requests = 25

        client = AlphaVantageClient(mock_http)
        result = await client.fetch_and_save_articles(sample_av_source, db_session)

    assert result.success is False
    assert "quota" in result.error_message.lower()
    mock_http.get.assert_not_called()
