"""ニュースフェッチャー オーケストレータのテスト。"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.collection.news_fetcher import fetch_news_for_sources
from app.models.news_source import NewsSource


def _make_feed(entries: list[dict], bozo: bool = False) -> MagicMock:
    """feedparser の FeedParserDict モックを作成する。"""
    feed = MagicMock()
    feed.entries = entries
    feed.bozo = bozo
    feed.bozo_exception = None if not bozo else Exception("parse error")
    return feed


def _make_entry(
    title: str = "Test Article",
    link: str = "https://example.com/article-1",
    summary: str = "Test description",
    guid: str | None = None,
    published_parsed: time.struct_time | None = None,
) -> dict:
    """RSS フィードエントリのモックを作成する。"""
    entry: dict = {"title": title, "link": link, "summary": summary}
    if guid:
        entry["id"] = guid
    else:
        entry["id"] = link
    if published_parsed:
        entry["published_parsed"] = published_parsed
    return entry


def _mock_response(
    status_code: int = 200,
    text: str = "",
    headers: dict | None = None,
) -> httpx.Response:
    """httpx レスポンスのモックを作成する。"""
    return httpx.Response(
        status_code=status_code,
        text=text,
        headers=headers or {},
        request=httpx.Request("GET", "https://example.com"),
    )


@pytest.fixture
def mock_client() -> AsyncMock:
    """モック httpx.AsyncClient を提供する。"""
    client = AsyncMock(spec=httpx.AsyncClient)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


async def test_fetch_with_empty_sources(db_session: AsyncSession) -> None:
    result = await fetch_news_for_sources(db_session, [])
    assert result.new_count == 0
    assert result.skipped_count == 0
    assert result.error_count == 0


async def test_fetch_populates_new_article_ids(
    db_session: AsyncSession, sample_source: NewsSource, mock_client: AsyncMock
) -> None:
    """new_article_ids には新規作成された全記事の ID が含まれる。"""
    entries = [
        _make_entry(title="A1", link="https://example.com/a1"),
        _make_entry(title="A2", link="https://example.com/a2"),
    ]
    feed = _make_feed(entries)
    mock_client.get.return_value = _mock_response(text="<rss>mock</rss>")

    with (
        patch(
            "app.collection.news_fetcher.httpx.AsyncClient", return_value=mock_client
        ),
        patch("app.collection.rss_fetcher.feedparser.parse", return_value=feed),
        patch(
            "app.collection.rss_fetcher.get_http_cache",
            new_callable=AsyncMock,
            return_value=(None, None),
        ),
        patch("app.collection.rss_fetcher.set_http_cache", new_callable=AsyncMock),
    ):
        result = await fetch_news_for_sources(db_session, [sample_source])

    assert len(result.new_article_ids) == 2
    assert result.content_ready_ids == []
    for aid in result.new_article_ids:
        assert isinstance(aid, int)
        assert aid > 0
