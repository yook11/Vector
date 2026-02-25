"""Tests for the news fetcher service."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.associations import NewsKeyword
from app.models.keyword import Keyword
from app.models.news import NewsArticle
from app.services.news_fetcher import (
    _build_rss_url,
    _parse_published_date,
    fetch_news_for_keywords,
)


def _passthrough_decode(urls: list[str]) -> dict[str, str]:
    """Return identity mapping (no decoding)."""
    return {u: u for u in urls}


@pytest.fixture(autouse=True)
def _mock_decode_urls():
    """Mock decode_urls to passthrough by default in all fetcher tests."""
    with patch(
        "app.services.news_fetcher.decode_urls",
        new_callable=AsyncMock,
        side_effect=lambda urls, **kw: _passthrough_decode(urls),
    ) as m:
        yield m


def _make_feed(entries: list[dict], bozo: bool = False) -> MagicMock:
    """Create a mock feedparser FeedParserDict."""
    feed = MagicMock()
    feed.entries = entries
    feed.bozo = bozo
    feed.bozo_exception = None if not bozo else Exception("parse error")
    return feed


def _make_entry(
    title: str = "Test Article",
    link: str = "https://example.com/article-1",
    summary: str = "Test description",
    published_parsed: time.struct_time | None = None,
) -> dict:
    """Create a mock RSS feed entry."""
    entry: dict = {"title": title, "link": link, "summary": summary}
    if published_parsed:
        entry["published_parsed"] = published_parsed
    return entry


# --- Unit tests ---


def test_build_rss_url() -> None:
    url = _build_rss_url("Quantum Computing")
    assert "Quantum%20Computing" in url
    assert url.startswith("https://news.google.com/rss/search?q=")


def test_parse_published_date_with_valid_struct() -> None:
    # 2025-01-15 12:00:00 UTC
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


# --- Integration tests (with DB) ---


async def test_fetch_saves_new_articles(
    db_session: AsyncSession, sample_keyword: Keyword
) -> None:
    entries = [
        _make_entry(title="Article 1", link="https://example.com/1"),
        _make_entry(title="Article 2", link="https://example.com/2"),
    ]
    feed = _make_feed(entries)

    with patch(
        "app.services.news_fetcher._fetch_rss_feed",
        new_callable=AsyncMock,
        return_value=feed,
    ):
        result = await fetch_news_for_keywords(db_session, [sample_keyword])

    assert result.new_count == 2
    assert result.skipped_count == 0

    articles = (await db_session.execute(select(NewsArticle))).scalars().all()
    assert len(articles) == 2

    links = (await db_session.execute(select(NewsKeyword))).scalars().all()
    assert len(links) == 2


async def test_fetch_skips_duplicate_urls(
    db_session: AsyncSession, sample_keyword: Keyword
) -> None:
    # Pre-insert an existing article
    existing = NewsArticle(
        title_original="Existing",
        url="https://example.com/existing",
        source="Google News",
    )
    db_session.add(existing)
    await db_session.commit()

    entries = [
        _make_entry(title="Existing", link="https://example.com/existing"),
        _make_entry(title="New One", link="https://example.com/new"),
    ]
    feed = _make_feed(entries)

    with patch(
        "app.services.news_fetcher._fetch_rss_feed",
        new_callable=AsyncMock,
        return_value=feed,
    ):
        result = await fetch_news_for_keywords(db_session, [sample_keyword])

    assert result.new_count == 1
    assert result.skipped_count == 1

    articles = (await db_session.execute(select(NewsArticle))).scalars().all()
    assert len(articles) == 2


async def test_fetch_links_multiple_keywords(db_session: AsyncSession) -> None:
    kw1 = Keyword(keyword="Keyword A", category="test", is_active=True)
    kw2 = Keyword(keyword="Keyword B", category="test", is_active=True)
    db_session.add_all([kw1, kw2])
    await db_session.commit()
    await db_session.refresh(kw1)
    await db_session.refresh(kw2)

    entries = [_make_entry(title="Shared", link="https://example.com/shared")]
    feed = _make_feed(entries)

    with patch(
        "app.services.news_fetcher._fetch_rss_feed",
        new_callable=AsyncMock,
        return_value=feed,
    ):
        result = await fetch_news_for_keywords(db_session, [kw1, kw2])

    assert result.new_count == 1

    links = (await db_session.execute(select(NewsKeyword))).scalars().all()
    assert len(links) == 2
    keyword_ids = {link.keyword_id for link in links}
    assert keyword_ids == {kw1.id, kw2.id}


async def test_fetch_handles_rss_error(
    db_session: AsyncSession, sample_keyword: Keyword
) -> None:
    with patch(
        "app.services.news_fetcher._fetch_rss_feed",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = await fetch_news_for_keywords(db_session, [sample_keyword])

    assert result.new_count == 0
    assert result.error_count == 1
    assert len(result.errors) == 1


async def test_fetch_respects_max_articles_limit(
    db_session: AsyncSession, sample_keyword: Keyword
) -> None:
    entries = [
        _make_entry(title=f"Article {i}", link=f"https://example.com/{i}")
        for i in range(60)
    ]
    feed = _make_feed(entries)

    with (
        patch(
            "app.services.news_fetcher._fetch_rss_feed",
            new_callable=AsyncMock,
            return_value=feed,
        ),
        patch("app.services.news_fetcher.settings") as mock_settings,
    ):
        mock_settings.max_articles_per_fetch = 50
        result = await fetch_news_for_keywords(db_session, [sample_keyword])

    assert result.new_count == 50


async def test_fetch_with_empty_keywords(db_session: AsyncSession) -> None:
    result = await fetch_news_for_keywords(db_session, [])
    assert result.new_count == 0
    assert result.skipped_count == 0
    assert result.error_count == 0


# --- URL decoding integration tests ---


async def test_fetch_decodes_google_news_urls(
    db_session: AsyncSession, sample_keyword: Keyword, _mock_decode_urls: AsyncMock
) -> None:
    """Google News URLs should be decoded to real article URLs before storage."""
    google_url = "https://news.google.com/rss/articles/CBMiSGh0dHBz"
    real_url = "https://www.reuters.com/real-article"

    entries = [_make_entry(title="Decoded Article", link=google_url)]
    feed = _make_feed(entries)

    # Override decode_urls to return the decoded URL
    _mock_decode_urls.side_effect = None
    _mock_decode_urls.return_value = {google_url: real_url}

    with patch(
        "app.services.news_fetcher._fetch_rss_feed",
        new_callable=AsyncMock,
        return_value=feed,
    ):
        result = await fetch_news_for_keywords(db_session, [sample_keyword])

    assert result.new_count == 1

    articles = (await db_session.execute(select(NewsArticle))).scalars().all()
    assert len(articles) == 1
    assert articles[0].url == real_url  # Stored the decoded URL, not the Google one

    # decode_urls should only be called with the new URL (not existing ones)
    _mock_decode_urls.assert_called_once_with([google_url])


async def test_fetch_merges_keywords_for_same_decoded_url(
    db_session: AsyncSession, _mock_decode_urls: AsyncMock
) -> None:
    """Two Google News URLs decoding to same real URL should merge."""
    kw1 = Keyword(keyword="AI", category="tech", is_active=True)
    kw2 = Keyword(keyword="ML", category="tech", is_active=True)
    db_session.add_all([kw1, kw2])
    await db_session.commit()
    await db_session.refresh(kw1)
    await db_session.refresh(kw2)

    google_url_1 = "https://news.google.com/rss/articles/CBMi111"
    google_url_2 = "https://news.google.com/rss/articles/CBMi222"
    real_url = "https://www.reuters.com/same-article"

    feed1 = _make_feed([_make_entry(title="Same Article", link=google_url_1)])
    feed2 = _make_feed([_make_entry(title="Same Article", link=google_url_2)])

    # Both Google URLs decode to the same real URL
    _mock_decode_urls.side_effect = None
    _mock_decode_urls.return_value = {
        google_url_1: real_url,
        google_url_2: real_url,
    }

    call_count = 0

    async def _rss_side_effect(client, url):
        nonlocal call_count
        result = feed1 if call_count == 0 else feed2
        call_count += 1
        return result

    with patch(
        "app.services.news_fetcher._fetch_rss_feed",
        new_callable=AsyncMock,
        side_effect=_rss_side_effect,
    ):
        result = await fetch_news_for_keywords(db_session, [kw1, kw2])

    # Should create only 1 article (not 2)
    assert result.new_count == 1

    articles = (await db_session.execute(select(NewsArticle))).scalars().all()
    assert len(articles) == 1
    assert articles[0].url == real_url

    # Both keywords should be linked
    links = (await db_session.execute(select(NewsKeyword))).scalars().all()
    assert len(links) == 2
    keyword_ids = {link.keyword_id for link in links}
    assert keyword_ids == {kw1.id, kw2.id}


async def test_fetch_skips_decode_for_existing_urls(
    db_session: AsyncSession, sample_keyword: Keyword, _mock_decode_urls: AsyncMock
) -> None:
    """URLs already in DB should not be passed to decode_urls."""
    existing_url = "https://news.google.com/rss/articles/existing"
    new_url = "https://news.google.com/rss/articles/new"

    # Pre-insert article with the existing Google News URL
    existing_article = NewsArticle(
        title_original="Existing",
        url=existing_url,
        source="Google News",
    )
    db_session.add(existing_article)
    await db_session.commit()

    entries = [
        _make_entry(title="Existing", link=existing_url),
        _make_entry(title="New", link=new_url),
    ]
    feed = _make_feed(entries)

    with patch(
        "app.services.news_fetcher._fetch_rss_feed",
        new_callable=AsyncMock,
        return_value=feed,
    ):
        result = await fetch_news_for_keywords(db_session, [sample_keyword])

    assert result.new_count == 1
    assert result.skipped_count == 1

    # decode_urls should only receive the new URL, not the existing one
    _mock_decode_urls.assert_called_once_with([new_url])


async def test_fetch_skips_decoded_url_already_in_db(
    db_session: AsyncSession, sample_keyword: Keyword, _mock_decode_urls: AsyncMock
) -> None:
    """A new Google News URL that decodes to an existing real URL should be skipped."""
    google_url = "https://news.google.com/rss/articles/CBMiNEW"
    real_url = "https://www.reuters.com/already-exists"

    # Pre-insert article with the real (decoded) URL
    existing_article = NewsArticle(
        title_original="Already Exists",
        url=real_url,
        source="Google News",
    )
    db_session.add(existing_article)
    await db_session.commit()

    entries = [_make_entry(title="Decoded to Existing", link=google_url)]
    feed = _make_feed(entries)

    # Google News URL decodes to the already-existing real URL
    _mock_decode_urls.side_effect = None
    _mock_decode_urls.return_value = {google_url: real_url}

    with patch(
        "app.services.news_fetcher._fetch_rss_feed",
        new_callable=AsyncMock,
        return_value=feed,
    ):
        result = await fetch_news_for_keywords(db_session, [sample_keyword])

    assert result.new_count == 0
    assert result.skipped_count == 1  # post-decode duplicate

    # No new articles created
    articles = (await db_session.execute(select(NewsArticle))).scalars().all()
    assert len(articles) == 1
    assert articles[0].url == real_url
