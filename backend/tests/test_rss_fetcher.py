"""RSS フェッチャーのテスト。"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.collection.rss_fetcher import (
    _extract_guid,
    _parse_published_date,
    fetch_rss_source,
)
from app.models.news_article import NewsArticle
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
        entry["id"] = link  # feedparser は <guid> を entry.id にマップする
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
    return client


# --- Unit tests ---


def test_parse_published_date_with_valid_struct() -> None:
    ts = time.struct_time((2025, 1, 15, 12, 0, 0, 2, 15, 0))
    result = _parse_published_date({"published_parsed": ts})
    assert result is not None
    assert result.year == 2025
    assert result.month == 1
    assert result.day == 15


def test_parse_published_date_with_missing_field() -> None:
    result = _parse_published_date({})
    assert result is None


def test_parse_published_date_falls_back_to_updated() -> None:
    ts = time.struct_time((2025, 6, 1, 0, 0, 0, 6, 152, 0))
    result = _parse_published_date({"updated_parsed": ts})
    assert result is not None
    assert result.month == 6


def test_extract_guid_prefers_id() -> None:
    entry = {"id": "urn:uuid:12345", "link": "https://example.com/article"}
    assert _extract_guid(entry) == "urn:uuid:12345"


def test_extract_guid_falls_back_to_link() -> None:
    entry = {"link": "https://example.com/article"}
    assert _extract_guid(entry) == "https://example.com/article"


def test_extract_guid_returns_none_for_empty() -> None:
    assert _extract_guid({}) is None


# --- Integration tests (with DB) ---


async def test_rss_saves_new_articles(
    db_session: AsyncSession, sample_source: NewsSource, mock_client: AsyncMock
) -> None:
    entries = [
        _make_entry(title="Article 1", link="https://example.com/1"),
        _make_entry(title="Article 2", link="https://example.com/2"),
    ]
    feed = _make_feed(entries)
    mock_client.get.return_value = _mock_response(text="<rss>mock</rss>")

    with (
        patch("app.collection.rss_fetcher.feedparser.parse", return_value=feed),
        patch(
            "app.collection.rss_fetcher.get_http_cache",
            new_callable=AsyncMock,
            return_value=(None, None),
        ),
        patch("app.collection.rss_fetcher.set_http_cache", new_callable=AsyncMock),
    ):
        result = await fetch_rss_source(mock_client, db_session, sample_source)

    assert result.new_count == 2
    assert result.skipped_count == 0

    await db_session.flush()
    articles = (await db_session.execute(select(NewsArticle))).scalars().all()
    assert len(articles) == 2
    assert all(a.news_source_id == sample_source.id for a in articles)
    assert all(a.original_url is not None for a in articles)
    assert all(a.original_title is not None for a in articles)


async def test_rss_skips_duplicate_urls(
    db_session: AsyncSession, sample_source: NewsSource, mock_client: AsyncMock
) -> None:
    existing = NewsArticle(
        original_title="Existing",
        original_url="https://example.com/existing",
        news_source_id=sample_source.id,
    )
    db_session.add(existing)
    await db_session.commit()

    entries = [
        _make_entry(title="Existing", link="https://example.com/existing"),
        _make_entry(title="New One", link="https://example.com/new"),
    ]
    feed = _make_feed(entries)
    mock_client.get.return_value = _mock_response(text="<rss>mock</rss>")

    with (
        patch("app.collection.rss_fetcher.feedparser.parse", return_value=feed),
        patch(
            "app.collection.rss_fetcher.get_http_cache",
            new_callable=AsyncMock,
            return_value=(None, None),
        ),
        patch("app.collection.rss_fetcher.set_http_cache", new_callable=AsyncMock),
    ):
        result = await fetch_rss_source(mock_client, db_session, sample_source)

    assert result.new_count == 1
    assert result.skipped_count == 1

    await db_session.flush()
    articles = (await db_session.execute(select(NewsArticle))).scalars().all()
    assert len(articles) == 2


async def test_rss_handles_304_not_modified(
    db_session: AsyncSession, sample_source: NewsSource, mock_client: AsyncMock
) -> None:
    mock_client.get.return_value = _mock_response(status_code=304)

    with patch(
        "app.collection.rss_fetcher.get_http_cache",
        new_callable=AsyncMock,
        return_value=(None, None),
    ):
        result = await fetch_rss_source(mock_client, db_session, sample_source)

    assert result.new_count == 0
    assert result.not_modified is True


async def test_rss_handles_http_error(
    db_session: AsyncSession, sample_source: NewsSource, mock_client: AsyncMock
) -> None:
    mock_client.get.return_value = _mock_response(status_code=500)

    with patch(
        "app.collection.rss_fetcher.get_http_cache",
        new_callable=AsyncMock,
        return_value=(None, None),
    ):
        result = await fetch_rss_source(mock_client, db_session, sample_source)

    assert result.new_count == 0
    assert result.success is False
    assert result.error_message is not None


async def test_rss_respects_max_articles_limit(
    db_session: AsyncSession, sample_source: NewsSource, mock_client: AsyncMock
) -> None:
    entries = [
        _make_entry(title=f"Article {i}", link=f"https://example.com/{i}")
        for i in range(60)
    ]
    feed = _make_feed(entries)
    mock_client.get.return_value = _mock_response(text="<rss>mock</rss>")

    with (
        patch("app.collection.rss_fetcher.feedparser.parse", return_value=feed),
        patch("app.collection.rss_fetcher.settings") as mock_settings,
        patch(
            "app.collection.rss_fetcher.get_http_cache",
            new_callable=AsyncMock,
            return_value=(None, None),
        ),
        patch("app.collection.rss_fetcher.set_http_cache", new_callable=AsyncMock),
    ):
        mock_settings.max_articles_per_fetch = 50
        mock_settings.content_max_length = 8000
        result = await fetch_rss_source(mock_client, db_session, sample_source)

    assert result.new_count == 50


async def test_rss_sends_conditional_get_headers(
    db_session: AsyncSession,
    sample_source: NewsSource,
    mock_client: AsyncMock,
) -> None:
    """ETag と Last-Modified は Redis から読み出しヘッダーとして送信する。"""
    mock_client.get.return_value = _mock_response(status_code=304)

    with patch(
        "app.collection.rss_fetcher.get_http_cache",
        new_callable=AsyncMock,
        return_value=('"abc123"', "Wed, 01 Jan 2025 00:00:00 GMT"),
    ):
        await fetch_rss_source(mock_client, db_session, sample_source)

    call_kwargs = mock_client.get.call_args
    headers = call_kwargs.kwargs.get("headers", {})
    assert headers["If-None-Match"] == '"abc123"'
    assert headers["If-Modified-Since"] == "Wed, 01 Jan 2025 00:00:00 GMT"


async def test_rss_captures_etag_and_writes_to_redis(
    db_session: AsyncSession,
    sample_source: NewsSource,
    mock_client: AsyncMock,
) -> None:
    """レスポンスの ETag と Last-Modified は Redis に書き込む。"""
    entries = [_make_entry(title="Art", link="https://example.com/art")]
    feed = _make_feed(entries)
    mock_client.get.return_value = _mock_response(
        text="<rss>mock</rss>",
        headers={
            "ETag": '"new-etag"',
            "Last-Modified": "Thu, 02 Jan 2025 00:00:00 GMT",
        },
    )

    with (
        patch("app.collection.rss_fetcher.feedparser.parse", return_value=feed),
        patch(
            "app.collection.rss_fetcher.get_http_cache",
            new_callable=AsyncMock,
            return_value=(None, None),
        ),
        patch(
            "app.collection.rss_fetcher.set_http_cache",
            new_callable=AsyncMock,
        ) as mock_set_cache,
    ):
        result = await fetch_rss_source(mock_client, db_session, sample_source)

    assert result.etag == '"new-etag"'
    assert result.last_modified == "Thu, 02 Jan 2025 00:00:00 GMT"
    mock_set_cache.assert_called_once_with(
        sample_source.id, '"new-etag"', "Thu, 02 Jan 2025 00:00:00 GMT"
    )


async def test_rss_stores_full_content(
    db_session: AsyncSession,
    sample_source: NewsSource,
    mock_client: AsyncMock,
) -> None:
    """RSS エントリに全文 (>500 文字) があれば即座に保存する。"""
    long_content = "A" * 600
    entry = _make_entry(
        title="Full Content",
        link="https://example.com/full",
        published_parsed=time.gmtime(1700000000),
    )
    entry["content"] = [{"value": long_content, "type": "text/html"}]

    feed = _make_feed([entry])
    mock_client.get.return_value = _mock_response(text="<rss>mock</rss>")

    with (
        patch("app.collection.rss_fetcher.feedparser.parse", return_value=feed),
        patch(
            "app.collection.rss_fetcher.get_http_cache",
            new_callable=AsyncMock,
            return_value=(None, None),
        ),
        patch("app.collection.rss_fetcher.set_http_cache", new_callable=AsyncMock),
    ):
        result = await fetch_rss_source(mock_client, db_session, sample_source)

    assert result.new_count == 1
    await db_session.flush()
    articles = (await db_session.execute(select(NewsArticle))).scalars().all()
    assert articles[0].original_content is not None
