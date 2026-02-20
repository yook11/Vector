"""Tests for the news fetcher service."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

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
