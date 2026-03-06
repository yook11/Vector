"""Tests for FetchLog recording in news_fetcher."""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.fetch_log import FetchLog
from app.models.news_source import NewsSource
from app.services.news_fetcher import fetch_news_for_sources


@pytest.mark.asyncio
async def test_fetch_log_recorded_on_success(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """Successful RSS fetch records a FetchLog with status='success'."""
    rss_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
    <channel>
        <title>Test</title>
        <item>
            <title>Article 1</title>
            <link>https://example.com/article-1</link>
            <guid>guid-1</guid>
        </item>
    </channel>
    </rss>"""

    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.text = rss_xml
    mock_response.headers = {}
    mock_response.raise_for_status = lambda: None

    with patch("app.services.news_fetcher.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        await fetch_news_for_sources(db_session, [sample_source])

    stmt = select(FetchLog).where(FetchLog.source_id == sample_source.id)
    result = await db_session.execute(stmt)
    log = result.scalar_one()

    assert log.status == "success"
    assert log.articles_count == 1
    assert log.error_message is None
    assert log.duration_ms is not None
    assert log.duration_ms >= 0


@pytest.mark.asyncio
async def test_fetch_log_recorded_on_error(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """Failed RSS fetch records a FetchLog with status='error'."""
    import httpx

    mock_response = AsyncMock()
    mock_response.status_code = 500

    with patch("app.services.news_fetcher.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "Server Error",
                request=httpx.Request("GET", sample_source.feed_url),
                response=httpx.Response(500),
            )
        )
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        await fetch_news_for_sources(db_session, [sample_source])

    stmt = select(FetchLog).where(FetchLog.source_id == sample_source.id)
    result = await db_session.execute(stmt)
    log = result.scalar_one()

    assert log.status == "error"
    assert log.articles_count == 0
    assert log.error_message == "HTTP 500"
    assert log.duration_ms is not None


@pytest.mark.asyncio
async def test_fetch_log_not_created_when_no_sources(
    db_session: AsyncSession,
) -> None:
    """No FetchLog is created when sources list is empty."""
    await fetch_news_for_sources(db_session, [])

    stmt = select(FetchLog)
    result = await db_session.execute(stmt)
    assert result.scalars().all() == []
