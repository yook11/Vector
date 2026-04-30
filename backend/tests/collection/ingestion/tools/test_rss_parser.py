"""``RssParser`` のユニットテスト (DB 非依存)。"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.ingestion.tools.rss_parser import RssParser

_MOD = "app.collection.ingestion.tools.rss_parser"


def _make_feed(entries: list[dict], bozo: bool = False) -> MagicMock:
    feed = MagicMock()
    feed.entries = entries
    feed.bozo = bozo
    feed.bozo_exception = None if not bozo else Exception("parse error")
    return feed


def _make_entry(
    title: str = "Test Article",
    link: str = "https://example.com/article-1",
    summary: str | None = "Test description",
    guid: str | None = None,
    published_parsed: time.struct_time | None = None,
    content_encoded: str | None = None,
) -> dict:
    entry: dict = {"title": title, "link": link}
    if summary is not None:
        entry["summary"] = summary
    entry["id"] = guid if guid else link
    if published_parsed:
        entry["published_parsed"] = published_parsed
    if content_encoded:
        entry["content"] = [{"value": content_encoded, "type": "text/html"}]
    return entry


def _mock_response(
    status_code: int = 200,
    text: str = "",
    headers: dict | None = None,
) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        text=text,
        headers=headers or {},
        request=httpx.Request("GET", "https://example.com"),
    )


@pytest.fixture
def mock_client() -> AsyncMock:
    return AsyncMock(spec=httpx.AsyncClient)


def _sample_source() -> MagicMock:
    source = MagicMock()
    source.id = 1
    source.name = "Test Tech Source"
    source.endpoint_url = "https://example.com/feed.xml"
    return source


class TestRssParser:
    async def test_returns_normalized_entries(self, mock_client: AsyncMock) -> None:
        entries = [
            _make_entry(title="Article 1", link="https://example.com/1"),
            _make_entry(title="Article 2", link="https://example.com/2"),
        ]
        feed = _make_feed(entries)
        mock_client.get.return_value = _mock_response(text="<rss>mock</rss>")

        with (
            patch(f"{_MOD}.feedparser.parse", return_value=feed),
            patch(
                f"{_MOD}.get_http_cache",
                new_callable=AsyncMock,
                return_value=(None, None),
            ),
            patch(f"{_MOD}.set_http_cache", new_callable=AsyncMock),
        ):
            results = await RssParser(mock_client).fetch_and_parse(_sample_source())

        assert len(results) == 2
        titles = [e.title for e in results]
        assert titles == ["Article 1", "Article 2"]

    async def test_extracts_published_from_published_parsed(
        self, mock_client: AsyncMock
    ) -> None:
        published = time.struct_time((2026, 4, 30, 12, 0, 0, 0, 0, 0))
        entries = [_make_entry(published_parsed=published)]
        feed = _make_feed(entries)
        mock_client.get.return_value = _mock_response(text="<rss>mock</rss>")

        with (
            patch(f"{_MOD}.feedparser.parse", return_value=feed),
            patch(
                f"{_MOD}.get_http_cache",
                new_callable=AsyncMock,
                return_value=(None, None),
            ),
            patch(f"{_MOD}.set_http_cache", new_callable=AsyncMock),
        ):
            results = await RssParser(mock_client).fetch_and_parse(_sample_source())

        assert results[0].published is not None
        assert results[0].published.year == 2026
        assert results[0].published.month == 4
        assert results[0].published.day == 30

    async def test_extracts_content_encoded(self, mock_client: AsyncMock) -> None:
        entries = [_make_entry(content_encoded="<p>Body HTML</p>")]
        feed = _make_feed(entries)
        mock_client.get.return_value = _mock_response(text="<rss>mock</rss>")

        with (
            patch(f"{_MOD}.feedparser.parse", return_value=feed),
            patch(
                f"{_MOD}.get_http_cache",
                new_callable=AsyncMock,
                return_value=(None, None),
            ),
            patch(f"{_MOD}.set_http_cache", new_callable=AsyncMock),
        ):
            results = await RssParser(mock_client).fetch_and_parse(_sample_source())

        assert results[0].content_encoded == "<p>Body HTML</p>"

    async def test_handles_304_not_modified(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = _mock_response(status_code=304)

        with patch(
            f"{_MOD}.get_http_cache",
            new_callable=AsyncMock,
            return_value=(None, None),
        ):
            results = await RssParser(mock_client).fetch_and_parse(_sample_source())

        assert results == []

    async def test_temporary_error_on_5xx(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = _mock_response(status_code=500)

        with patch(
            f"{_MOD}.get_http_cache",
            new_callable=AsyncMock,
            return_value=(None, None),
        ):
            with pytest.raises(TemporaryFetchError):
                await RssParser(mock_client).fetch_and_parse(_sample_source())

    async def test_permanent_error_on_404(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = _mock_response(status_code=404)

        with patch(
            f"{_MOD}.get_http_cache",
            new_callable=AsyncMock,
            return_value=(None, None),
        ):
            with pytest.raises(PermanentFetchError):
                await RssParser(mock_client).fetch_and_parse(_sample_source())

    async def test_temporary_error_on_network_failure(
        self, mock_client: AsyncMock
    ) -> None:
        mock_client.get.side_effect = httpx.ConnectError("connection refused")

        with patch(
            f"{_MOD}.get_http_cache",
            new_callable=AsyncMock,
            return_value=(None, None),
        ):
            with pytest.raises(TemporaryFetchError):
                await RssParser(mock_client).fetch_and_parse(_sample_source())

    async def test_permanent_error_on_bozo_feed(self, mock_client: AsyncMock) -> None:
        feed = _make_feed(entries=[], bozo=True)
        mock_client.get.return_value = _mock_response(text="<not-valid>")

        with (
            patch(f"{_MOD}.feedparser.parse", return_value=feed),
            patch(
                f"{_MOD}.get_http_cache",
                new_callable=AsyncMock,
                return_value=(None, None),
            ),
            patch(f"{_MOD}.set_http_cache", new_callable=AsyncMock),
        ):
            with pytest.raises(PermanentFetchError):
                await RssParser(mock_client).fetch_and_parse(_sample_source())

    async def test_sends_conditional_get_headers(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = _mock_response(status_code=304)

        with patch(
            f"{_MOD}.get_http_cache",
            new_callable=AsyncMock,
            return_value=('"abc123"', "Wed, 01 Jan 2025 00:00:00 GMT"),
        ):
            await RssParser(mock_client).fetch_and_parse(_sample_source())

        headers = mock_client.get.call_args.kwargs.get("headers", {})
        assert headers["If-None-Match"] == '"abc123"'
        assert headers["If-Modified-Since"] == "Wed, 01 Jan 2025 00:00:00 GMT"

    async def test_writes_etag_to_redis(self, mock_client: AsyncMock) -> None:
        entries = [_make_entry(title="Art", link="https://example.com/art")]
        feed = _make_feed(entries)
        source = _sample_source()
        mock_client.get.return_value = _mock_response(
            text="<rss>mock</rss>",
            headers={
                "ETag": '"new-etag"',
                "Last-Modified": "Thu, 02 Jan 2025 00:00:00 GMT",
            },
        )

        with (
            patch(f"{_MOD}.feedparser.parse", return_value=feed),
            patch(
                f"{_MOD}.get_http_cache",
                new_callable=AsyncMock,
                return_value=(None, None),
            ),
            patch(
                f"{_MOD}.set_http_cache",
                new_callable=AsyncMock,
            ) as mock_set_cache,
        ):
            await RssParser(mock_client).fetch_and_parse(source)

        mock_set_cache.assert_called_once_with(
            source.id, '"new-etag"', "Thu, 02 Jan 2025 00:00:00 GMT"
        )

    async def test_falls_back_to_updated_parsed_when_published_missing(
        self, mock_client: AsyncMock
    ) -> None:
        entry = _make_entry(link="https://example.com/no-pub")
        entry["updated_parsed"] = time.struct_time((2026, 5, 1, 0, 0, 0, 0, 0, 0))
        feed = _make_feed([entry])
        mock_client.get.return_value = _mock_response(text="<rss>mock</rss>")

        with (
            patch(f"{_MOD}.feedparser.parse", return_value=feed),
            patch(
                f"{_MOD}.get_http_cache",
                new_callable=AsyncMock,
                return_value=(None, None),
            ),
            patch(f"{_MOD}.set_http_cache", new_callable=AsyncMock),
        ):
            results = await RssParser(mock_client).fetch_and_parse(_sample_source())

        assert results[0].published is not None
        assert results[0].published.month == 5
        assert results[0].published.day == 1
