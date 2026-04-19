"""Hacker News フェッチャーのテスト。"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.collection.ingestion.fetchers.hacker_news import HackerNewsFetcher, HNStory
from app.models.discovered_article import DiscoveredArticle
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
    """HN API 向けのモック httpx レスポンスを作成する。"""
    import json

    return httpx.Response(
        status_code=status_code,
        text=json.dumps(data or SAMPLE_HN_RESPONSE),
        headers={"content-type": "application/json"},
        request=httpx.Request("GET", "https://hn.algolia.com/api/v1/search_by_date"),
    )


@pytest.fixture
def mock_http_client() -> AsyncMock:
    """HN テスト用の httpx.AsyncClient モックを提供する。"""
    client = AsyncMock(spec=httpx.AsyncClient)
    return client


# --- HackerNewsFetcher._fetch_recent_stories tests ---


async def test_fetch_recent_stories_success(
    mock_http_client: AsyncMock,
) -> None:
    """Story を取得し url=None のエントリはフィルタ除外される。"""
    mock_http_client.get.return_value = _mock_hn_response()

    fetcher = HackerNewsFetcher()
    stories = await fetcher._fetch_recent_stories(mock_http_client)

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
    """since_timestamp は numericFilters に含まれる。"""
    mock_http_client.get.return_value = _mock_hn_response(data={"hits": []})

    fetcher = HackerNewsFetcher()
    await fetcher._fetch_recent_stories(mock_http_client, since_timestamp=1771953317)

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
    """since_timestamp がない場合 numericFilters は points フィルタのみ。"""
    mock_http_client.get.return_value = _mock_hn_response(data={"hits": []})

    fetcher = HackerNewsFetcher()
    await fetcher._fetch_recent_stories(mock_http_client)

    call_kwargs = mock_http_client.get.call_args
    params = call_kwargs.kwargs.get("params", {})
    numeric_filters = params.get("numericFilters", "")
    assert "points>" in numeric_filters
    assert "created_at_i>" not in numeric_filters


async def test_fetch_recent_stories_api_error(
    mock_http_client: AsyncMock,
) -> None:
    """HTTP エラーは HTTPStatusError として伝播する。"""
    mock_http_client.get.return_value = _mock_hn_response(data={}, status_code=429)

    fetcher = HackerNewsFetcher()
    with pytest.raises(httpx.HTTPStatusError):
        await fetcher._fetch_recent_stories(mock_http_client)


# --- HackerNewsFetcher.fetch tests ---


async def test_save_new_stories(
    db_session: AsyncSession,
    sample_hn_source: NewsSource,
    mock_http_client: AsyncMock,
) -> None:
    """新規 HN story は discovered_articles に保存される。"""
    mock_http_client.get.return_value = _mock_hn_response()

    fetcher = HackerNewsFetcher()
    result = await fetcher.fetch(
        client=mock_http_client, session=db_session, source=sample_hn_source
    )
    await db_session.commit()

    assert result.success is True
    assert result.new_count == 2
    assert result.skipped_count == 0

    articles = (await db_session.execute(select(DiscoveredArticle))).scalars().all()
    assert len(articles) == 2

    for article in articles:
        assert article.news_source_id == sample_hn_source.id
        assert article.original_url is not None
        assert article.original_title is not None


async def test_skip_duplicate_url(
    db_session: AsyncSession,
    sample_hn_source: NewsSource,
    mock_http_client: AsyncMock,
) -> None:
    """original_url が既存 (RSS 経由) の記事はスキップされる。"""
    existing = DiscoveredArticle(
        original_title="Same article from RSS",
        original_url="https://www.calebleak.com/posts/dog-game/",
        news_source_id=sample_hn_source.id,
    )
    db_session.add(existing)
    await db_session.commit()

    mock_http_client.get.return_value = _mock_hn_response()

    fetcher = HackerNewsFetcher()
    result = await fetcher.fetch(
        client=mock_http_client, session=db_session, source=sample_hn_source
    )
    await db_session.commit()

    assert result.new_count == 1
    assert result.skipped_count == 1


async def test_fetch_handles_http_error(
    db_session: AsyncSession,
    sample_hn_source: NewsSource,
    mock_http_client: AsyncMock,
) -> None:
    """HTTP エラー時は SourceFetchResult(success=False) となる。"""
    mock_http_client.get.return_value = _mock_hn_response(data={}, status_code=500)

    fetcher = HackerNewsFetcher()
    result = await fetcher.fetch(
        client=mock_http_client, session=db_session, source=sample_hn_source
    )

    assert result.success is False
    assert "HTTP 500" in result.error_message


async def test_fetch_handles_network_error(
    db_session: AsyncSession,
    sample_hn_source: NewsSource,
    mock_http_client: AsyncMock,
) -> None:
    """ネットワークエラー時は SourceFetchResult(success=False) となる。"""
    mock_http_client.get.side_effect = httpx.ConnectError("Connection refused")

    fetcher = HackerNewsFetcher()
    result = await fetcher.fetch(
        client=mock_http_client, session=db_session, source=sample_hn_source
    )

    assert result.success is False
    assert result.error_message is not None


async def test_fetch_with_last_fetched_at(
    db_session: AsyncSession,
    sample_hn_source: NewsSource,
    mock_http_client: AsyncMock,
) -> None:
    """fetch_logs から導出した last_fetched_at が API フィルタに使われる。"""
    from app.models.fetch_log import FetchLog, FetchStatus

    # 成功した fetch ログを 1 件作成
    log = FetchLog(
        source_id=sample_hn_source.id,
        status=FetchStatus.SUCCESS,
        articles_count=5,
        fetched_at=datetime(2026, 2, 24, 17, 0, 0, tzinfo=UTC),
    )
    db_session.add(log)
    await db_session.commit()

    mock_http_client.get.return_value = _mock_hn_response(data={"hits": []})

    fetcher = HackerNewsFetcher()
    await fetcher.fetch(
        client=mock_http_client, session=db_session, source=sample_hn_source
    )

    call_kwargs = mock_http_client.get.call_args
    params = call_kwargs.kwargs.get("params", {})
    numeric_filters = params.get("numericFilters", "")
    assert "created_at_i>" in numeric_filters


async def test_fetch_empty_response(
    db_session: AsyncSession,
    sample_hn_source: NewsSource,
    mock_http_client: AsyncMock,
) -> None:
    """API レスポンスが空でも success=True かつ new_count=0 を返す。"""
    mock_http_client.get.return_value = _mock_hn_response(data={"hits": []})

    fetcher = HackerNewsFetcher()
    result = await fetcher.fetch(
        client=mock_http_client, session=db_session, source=sample_hn_source
    )

    assert result.success is True
    assert result.new_count == 0
    assert result.skipped_count == 0
