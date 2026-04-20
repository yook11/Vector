"""QuantumInsiderFetcher の convert_entry テスト。"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.collection.ingestion.fetchers.rss.quantum_insider import (
    QuantumInsiderFetcher,
)
from app.models.discovered_article import DiscoveredArticle
from app.models.news_source import NewsSource

_BASE_MOD = "app.collection.ingestion.fetchers.rss.base"


class TestQuantumInsiderConvertEntry:
    def test_extracts_full_content(self) -> None:
        """content:encoded があれば content フィールドに格納する。"""
        full_content = "A" * 600
        entry = {
            "link": "https://thequantuminsider.com/article-1",
            "title": "Quantum News",
            "summary": "Short summary",
            "content": [{"value": full_content, "type": "text/html"}],
        }
        fetcher = QuantumInsiderFetcher()
        candidate = fetcher.convert_entry(entry)

        assert candidate is not None
        assert candidate.content == full_content

    def test_content_none_when_no_content_encoded(self) -> None:
        """content:encoded がなければ content は None。"""
        entry = {
            "link": "https://thequantuminsider.com/article-2",
            "title": "Quantum News 2",
            "summary": "Summary",
        }
        fetcher = QuantumInsiderFetcher()
        candidate = fetcher.convert_entry(entry)

        assert candidate is not None
        assert candidate.content is None

    def test_returns_none_for_empty_url(self) -> None:
        entry = {"link": "", "title": "No URL", "summary": "No URL"}
        fetcher = QuantumInsiderFetcher()
        assert fetcher.convert_entry(entry) is None


async def test_rss_stores_full_content(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """content:encoded の全文があれば original_content に保存される。"""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    long_content = "A" * 600
    entry = {
        "title": "Full Content",
        "link": "https://example.com/full",
        "summary": "Summary",
        "id": "https://example.com/full",
        "content": [{"value": long_content, "type": "text/html"}],
        "published_parsed": time.gmtime(1700000000),
    }

    feed = MagicMock()
    feed.entries = [entry]
    feed.bozo = False

    mock_client.get.return_value = httpx.Response(
        status_code=200,
        text="<rss>mock</rss>",
        request=httpx.Request("GET", "https://example.com"),
    )

    with (
        patch(f"{_BASE_MOD}.feedparser.parse", return_value=feed),
        patch(
            f"{_BASE_MOD}.get_http_cache",
            new_callable=AsyncMock,
            return_value=(None, None),
        ),
        patch(f"{_BASE_MOD}.set_http_cache", new_callable=AsyncMock),
    ):
        result = await QuantumInsiderFetcher().fetch(
            mock_client, db_session, sample_source
        )

    assert len(result.new_discovered) == 1
    await db_session.flush()
    articles = (await db_session.execute(select(DiscoveredArticle))).scalars().all()
    assert len(articles) == 1
    assert articles[0].original_title == "Full Content"
