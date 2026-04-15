"""Tests for the Hacker News fetcher service."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.collection.hacker_news import HackerNewsClient, HNStory
from app.models.news_article import NewsArticle
from app.models.news_source import NewsSource

# --- Sample API response data ---

SAMPLE_HN_RESPONSE = {
    "hits": [
        {
            "objectID": "47139675",
            "title": "I'm helping my dog vibe code games",
            "url": "https://www.calebleak.com/posts/dog-game/",
            "author": "cleak",
            "points": 1082,
            "num_comments": 365,
            "story_text": None,
            "created_at": "2026-02-24T17:15:17Z",
            "created_at_i": 1771953317,
            "_tags": ["story", "author_cleak", "story_47139675"],
        },
        {
            "objectID": "47140000",
            "title": "Ask HN: What are you working on?",
            "url": None,
            "author": "someone",
            "points": 200,
            "num_comments": 150,
            "story_text": "Some text post",
            "created_at": "2026-02-24T18:00:00Z",
            "created_at_i": 1771956000,
            "_tags": ["story"],
        },
        {
            "objectID": "47140001",
            "title": "New Rust release v2.0",
            "url": "https://blog.rust-lang.org/2026/02/24/rust-2.html",
            "author": "rustdev",
            "points": 500,
            "num_comments": 200,
            "story_text": None,
            "created_at": "2026-02-24T19:00:00Z",
            "created_at_i": 1771959600,
            "_tags": ["story"],
        },
    ],
    "nbHits": 3,
    "page": 0,
    "nbPages": 1,
    "hitsPerPage": 50,
}


def _mock_hn_response(
    data: dict | None = None,
    status_code: int = 200,
) -> httpx.Response:
    """Create a mock httpx response for HN API."""
    import json

    return httpx.Response(
        status_code=status_code,
        text=json.dumps(data or SAMPLE_HN_RESPONSE),
        headers={"content-type": "application/json"},
        request=httpx.Request("GET", "https://hn.algolia.com/api/v1/search_by_date"),
    )


@pytest.fixture
def mock_http_client() -> AsyncMock:
    """Provide a mock httpx.AsyncClient for HN tests."""
    client = AsyncMock(spec=httpx.AsyncClient)
    return client


# --- HackerNewsClient.fetch_recent_stories tests ---


async def test_fetch_recent_stories_success(
    mock_http_client: AsyncMock,
) -> None:
    """Stories are fetched and url=None entries are filtered out."""
    mock_http_client.get.return_value = _mock_hn_response()

    hn_client = HackerNewsClient(mock_http_client)
    stories = await hn_client.fetch_recent_stories()

    assert len(stories) == 2
    assert all(isinstance(s, HNStory) for s in stories)

    assert stories[0].object_id == "47139675"
    assert stories[0].title == "I'm helping my dog vibe code games"
    assert stories[0].url == "https://www.calebleak.com/posts/dog-game/"
    assert stories[0].points == 1082
    assert stories[0].created_at_i == 1771953317
    assert stories[0].author == "cleak"
    assert stories[0].num_comments == 365

    assert stories[1].object_id == "47140001"
    assert stories[1].title == "New Rust release v2.0"


async def test_fetch_recent_stories_with_since_timestamp(
    mock_http_client: AsyncMock,
) -> None:
    """since_timestamp should be included in numericFilters."""
    mock_http_client.get.return_value = _mock_hn_response(data={"hits": []})

    hn_client = HackerNewsClient(mock_http_client)
    await hn_client.fetch_recent_stories(since_timestamp=1771953317)

    call_kwargs = mock_http_client.get.call_args
    params = (
        call_kwargs.args[1]
        if len(call_kwargs.args) > 1
        else call_kwargs.kwargs.get("params", {})
    )
    numeric_filters = params.get("numericFilters", "")
    assert "created_at_i>1771953317" in numeric_filters


async def test_fetch_recent_stories_without_since_timestamp(
    mock_http_client: AsyncMock,
) -> None:
    """Without since_timestamp, numericFilters should only have points filter."""
    mock_http_client.get.return_value = _mock_hn_response(data={"hits": []})

    hn_client = HackerNewsClient(mock_http_client)
    await hn_client.fetch_recent_stories()

    call_kwargs = mock_http_client.get.call_args
    params = call_kwargs.kwargs.get("params", {})
    numeric_filters = params.get("numericFilters", "")
    assert "points>" in numeric_filters
    assert "created_at_i>" not in numeric_filters


async def test_fetch_recent_stories_api_error(
    mock_http_client: AsyncMock,
) -> None:
    """HTTP errors should propagate as HTTPStatusError."""
    mock_http_client.get.return_value = _mock_hn_response(data={}, status_code=429)

    hn_client = HackerNewsClient(mock_http_client)
    with pytest.raises(httpx.HTTPStatusError):
        await hn_client.fetch_recent_stories()


# --- HackerNewsClient.fetch_and_save_stories tests ---


async def test_save_new_stories(
    db_session: AsyncSession,
    sample_hn_source: NewsSource,
    mock_http_client: AsyncMock,
) -> None:
    """New HN stories should be saved to news_articles."""
    mock_http_client.get.return_value = _mock_hn_response()

    hn_client = HackerNewsClient(mock_http_client)
    result = await hn_client.fetch_and_save_stories(
        source=sample_hn_source, session=db_session
    )
    await db_session.commit()

    assert result.success is True
    assert result.new_count == 2
    assert result.skipped_count == 0

    articles = (await db_session.execute(select(NewsArticle))).scalars().all()
    assert len(articles) == 2

    for article in articles:
        assert article.news_source_id == sample_hn_source.id
        assert article.original_url is not None
        assert article.original_title is not None
        assert article.published_at is not None


async def test_skip_duplicate_url(
    db_session: AsyncSession,
    sample_hn_source: NewsSource,
    mock_http_client: AsyncMock,
) -> None:
    """Articles with existing original_url (from RSS) should be skipped."""
    existing = NewsArticle(
        original_title="Same article from RSS",
        original_url="https://www.calebleak.com/posts/dog-game/",
        news_source_id=sample_hn_source.id,
    )
    db_session.add(existing)
    await db_session.commit()

    mock_http_client.get.return_value = _mock_hn_response()

    hn_client = HackerNewsClient(mock_http_client)
    result = await hn_client.fetch_and_save_stories(
        source=sample_hn_source, session=db_session
    )
    await db_session.commit()

    assert result.new_count == 1
    assert result.skipped_count == 1


async def test_fetch_and_save_handles_http_error(
    db_session: AsyncSession,
    sample_hn_source: NewsSource,
    mock_http_client: AsyncMock,
) -> None:
    """HTTP errors should result in SourceFetchResult(success=False)."""
    mock_http_client.get.return_value = _mock_hn_response(data={}, status_code=500)

    hn_client = HackerNewsClient(mock_http_client)
    result = await hn_client.fetch_and_save_stories(
        source=sample_hn_source, session=db_session
    )

    assert result.success is False
    assert "HTTP 500" in result.error_message


async def test_fetch_and_save_handles_network_error(
    db_session: AsyncSession,
    sample_hn_source: NewsSource,
    mock_http_client: AsyncMock,
) -> None:
    """Network errors should result in SourceFetchResult(success=False)."""
    mock_http_client.get.side_effect = httpx.ConnectError("Connection refused")

    hn_client = HackerNewsClient(mock_http_client)
    result = await hn_client.fetch_and_save_stories(
        source=sample_hn_source, session=db_session
    )

    assert result.success is False
    assert result.error_message is not None


async def test_fetch_and_save_with_last_fetched_at(
    db_session: AsyncSession,
    sample_hn_source: NewsSource,
    mock_http_client: AsyncMock,
) -> None:
    """last_fetched_at derived from fetch_logs should be used as API filter."""
    from app.models.fetch_log import FetchLog, FetchStatus

    # Create a successful fetch log entry
    log = FetchLog(
        source_id=sample_hn_source.id,
        status=FetchStatus.SUCCESS,
        articles_count=5,
        fetched_at=datetime(2026, 2, 24, 17, 0, 0, tzinfo=UTC),
    )
    db_session.add(log)
    await db_session.commit()

    mock_http_client.get.return_value = _mock_hn_response(data={"hits": []})

    hn_client = HackerNewsClient(mock_http_client)
    await hn_client.fetch_and_save_stories(source=sample_hn_source, session=db_session)

    call_kwargs = mock_http_client.get.call_args
    params = call_kwargs.kwargs.get("params", {})
    numeric_filters = params.get("numericFilters", "")
    assert "created_at_i>" in numeric_filters


async def test_fetch_and_save_empty_response(
    db_session: AsyncSession,
    sample_hn_source: NewsSource,
    mock_http_client: AsyncMock,
) -> None:
    """Empty API response should return success with 0 new articles."""
    mock_http_client.get.return_value = _mock_hn_response(data={"hits": []})

    hn_client = HackerNewsClient(mock_http_client)
    result = await hn_client.fetch_and_save_stories(
        source=sample_hn_source, session=db_session
    )

    assert result.success is True
    assert result.new_count == 0
    assert result.skipped_count == 0
