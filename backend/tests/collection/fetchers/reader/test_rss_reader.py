"""``RssReader`` のユニットテスト (DB 非依存)。

新 API: SSRF guard 構造的内蔵、NewsSource 非依存、parse_mode 引数化、
title plain text 正規化を ``normalize_entry`` に集約。HTTP cache は廃止
(別 PR で復活予定)。
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.collection.external_fetch_errors import (
    FetchAccessDeniedError,
    FetchLegalBlockError,
    FetchNetworkError,
    FetchOriginServerError,
    FetchParseError,
    FetchRateLimitedError,
    FetchResourceNotFoundError,
    FetchSsrfBlockedError,
)
from app.collection.source_fetch.reader.rss_reader import (
    RssReader,
    normalize_entry,
)
from app.shared.security.ssrf_guard import HostBlockedError, HostResolutionError

_MOD = "app.collection.source_fetch.reader.rss_reader"

_ENDPOINT = "https://example.com/feed.xml"
_SOURCE = "Test Source"


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
    tags: list[str] | None = None,
    raw_published: str | None = None,
    raw_updated: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {"title": title, "link": link}
    if summary is not None:
        entry["summary"] = summary
    entry["id"] = guid if guid else link
    if published_parsed:
        entry["published_parsed"] = published_parsed
    if content_encoded:
        entry["content"] = [{"value": content_encoded, "type": "text/html"}]
    if tags is not None:
        entry["tags"] = [{"term": t, "scheme": None, "label": None} for t in tags]
    if raw_published is not None:
        entry["published"] = raw_published
    if raw_updated is not None:
        entry["updated"] = raw_updated
    return entry


def _mock_response(
    status_code: int = 200,
    text: str = "",
    content: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    if content is not None:
        return httpx.Response(
            status_code=status_code,
            content=content,
            headers=headers or {},
            request=httpx.Request("GET", _ENDPOINT),
        )
    return httpx.Response(
        status_code=status_code,
        text=text,
        headers=headers or {},
        request=httpx.Request("GET", _ENDPOINT),
    )


def _patch_safe_client(response_or_exc: httpx.Response | Exception) -> Any:
    """``make_safe_async_client`` を fake ``async with`` context に差し替える。"""

    @asynccontextmanager
    async def _fake_safe_client(**_kwargs: Any) -> Any:
        client = AsyncMock(spec=httpx.AsyncClient)
        if isinstance(response_or_exc, Exception):
            client.get = AsyncMock(side_effect=response_or_exc)
        else:
            client.get = AsyncMock(return_value=response_or_exc)
        yield client

    return patch(f"{_MOD}.make_safe_async_client", _fake_safe_client)


class TestNormalizeEntry:
    """``normalize_entry`` の単体テスト (HTTP / feedparser を経由しない)。"""

    def test_title_is_plain_text_normalized(self) -> None:
        entry = _make_entry(title="Foo &amp; Bar <span>baz</span>")
        result = normalize_entry(entry)
        assert result.title == "Foo & Bar baz"

    def test_title_with_multiline_whitespace_is_compressed(self) -> None:
        entry = _make_entry(title="Line 1\n\n  Line 2\t\tend")
        result = normalize_entry(entry)
        assert result.title == "Line 1 Line 2 end"

    def test_empty_title_is_empty_string(self) -> None:
        entry = _make_entry(title="")
        result = normalize_entry(entry)
        assert result.title == ""

    def test_extracts_published_from_published_parsed(self) -> None:
        published = time.struct_time((2026, 4, 30, 12, 0, 0, 0, 0, 0))
        entry = _make_entry(published_parsed=published)
        result = normalize_entry(entry)
        assert result.published is not None
        assert result.published.year == 2026
        assert result.published.month == 4
        assert result.published.day == 30
        assert result.published.tzinfo is not None

    def test_falls_back_to_updated_parsed_when_published_missing(self) -> None:
        entry = _make_entry(link="https://example.com/no-pub")
        entry["updated_parsed"] = time.struct_time((2026, 5, 1, 0, 0, 0, 0, 0, 0))
        result = normalize_entry(entry)
        assert result.published is not None
        assert result.published.month == 5
        assert result.published.day == 1

    def test_extracts_content_encoded(self) -> None:
        entry = _make_entry(content_encoded="<p>Body HTML</p>")
        result = normalize_entry(entry)
        assert result.content_encoded == "<p>Body HTML</p>"

    def test_content_encoded_is_none_when_absent(self) -> None:
        entry = _make_entry()
        result = normalize_entry(entry)
        assert result.content_encoded is None

    def test_summary_is_raw_html(self) -> None:
        entry = _make_entry(summary="<p>raw &amp; html</p>")
        result = normalize_entry(entry)
        assert result.summary == "<p>raw &amp; html</p>"

    def test_extracts_tags_from_categories(self) -> None:
        entry = _make_entry(tags=["AI", "Machine Learning"])
        result = normalize_entry(entry)
        assert result.tags == ("AI", "Machine Learning")

    def test_tags_empty_when_absent(self) -> None:
        entry = _make_entry()
        result = normalize_entry(entry)
        assert result.tags == ()

    def test_extracts_raw_published_and_raw_updated(self) -> None:
        entry = _make_entry(
            raw_published="Apr 30, 2026 6:11pm",
            raw_updated="May 01, 2026 8:00am",
        )
        result = normalize_entry(entry)
        assert result.raw_published == "Apr 30, 2026 6:11pm"
        assert result.raw_updated == "May 01, 2026 8:00am"

    def test_raw_published_and_raw_updated_none_when_absent(self) -> None:
        entry = _make_entry()
        result = normalize_entry(entry)
        assert result.raw_published is None
        assert result.raw_updated is None

    def test_guid_is_trimmed_to_2048(self) -> None:
        long_guid = "https://example.com/" + "x" * 3000
        entry = _make_entry(guid=long_guid)
        result = normalize_entry(entry)
        assert result.guid is not None
        assert len(result.guid) == 2048


class TestRssReaderFetch:
    """``RssReader.fetch`` の HTTP + feedparser 統合テスト。"""

    async def test_returns_normalized_entries(self) -> None:
        entries = [
            _make_entry(title="Article 1", link="https://example.com/1"),
            _make_entry(title="Article 2", link="https://example.com/2"),
        ]
        feed = _make_feed(entries)
        response = _mock_response(text="<rss>mock</rss>")

        with (
            _patch_safe_client(response),
            patch(f"{_MOD}.feedparser.parse", return_value=feed),
        ):
            results = await RssReader().fetch(
                endpoint_url=_ENDPOINT, source_name=_SOURCE
            )

        assert len(results) == 2
        assert [e.title for e in results] == ["Article 1", "Article 2"]

    async def test_text_mode_passes_response_text_to_feedparser(self) -> None:
        response = _mock_response(text="<rss>text-mode-payload</rss>")
        feed = _make_feed([])

        with (
            _patch_safe_client(response),
            patch(f"{_MOD}.feedparser.parse", return_value=feed) as mock_parse,
        ):
            await RssReader().fetch(
                endpoint_url=_ENDPOINT, source_name=_SOURCE, parse_mode="text"
            )

        mock_parse.assert_called_once_with("<rss>text-mode-payload</rss>")

    async def test_bytes_mode_passes_response_content_to_feedparser(self) -> None:
        payload = "<?xml version='1.0' encoding='Shift_JIS'?><rdf/>".encode("shift_jis")
        response = _mock_response(content=payload)
        feed = _make_feed([])

        with (
            _patch_safe_client(response),
            patch(f"{_MOD}.feedparser.parse", return_value=feed) as mock_parse,
        ):
            await RssReader().fetch(
                endpoint_url=_ENDPOINT, source_name=_SOURCE, parse_mode="bytes"
            )

        mock_parse.assert_called_once_with(payload)

    async def test_403_raises_access_denied(self) -> None:
        response = _mock_response(status_code=403)
        with _patch_safe_client(response):
            with pytest.raises(FetchAccessDeniedError):
                await RssReader().fetch(endpoint_url=_ENDPOINT, source_name=_SOURCE)

    async def test_404_raises_resource_not_found(self) -> None:
        response = _mock_response(status_code=404)
        with _patch_safe_client(response):
            with pytest.raises(FetchResourceNotFoundError):
                await RssReader().fetch(endpoint_url=_ENDPOINT, source_name=_SOURCE)

    async def test_410_raises_resource_not_found(self) -> None:
        response = _mock_response(status_code=410)
        with _patch_safe_client(response):
            with pytest.raises(FetchResourceNotFoundError):
                await RssReader().fetch(endpoint_url=_ENDPOINT, source_name=_SOURCE)

    async def test_451_raises_legal_block(self) -> None:
        response = _mock_response(status_code=451)
        with _patch_safe_client(response):
            with pytest.raises(FetchLegalBlockError):
                await RssReader().fetch(endpoint_url=_ENDPOINT, source_name=_SOURCE)

    async def test_500_raises_origin_server_error(self) -> None:
        response = _mock_response(status_code=500)
        with _patch_safe_client(response):
            with pytest.raises(FetchOriginServerError):
                await RssReader().fetch(endpoint_url=_ENDPOINT, source_name=_SOURCE)

    async def test_429_raises_rate_limited(self) -> None:
        response = _mock_response(status_code=429)
        with _patch_safe_client(response):
            with pytest.raises(FetchRateLimitedError):
                await RssReader().fetch(endpoint_url=_ENDPOINT, source_name=_SOURCE)

    async def test_request_error_raises_network(self) -> None:
        with _patch_safe_client(httpx.ConnectError("connection refused")):
            with pytest.raises(FetchNetworkError):
                await RssReader().fetch(endpoint_url=_ENDPOINT, source_name=_SOURCE)

    async def test_host_blocked_raises_ssrf_blocked(self) -> None:
        with _patch_safe_client(HostBlockedError("private IP literal")):
            with pytest.raises(FetchSsrfBlockedError):
                await RssReader().fetch(endpoint_url=_ENDPOINT, source_name=_SOURCE)

    async def test_host_resolution_raises_network(self) -> None:
        with _patch_safe_client(HostResolutionError("dns failure")):
            with pytest.raises(FetchNetworkError):
                await RssReader().fetch(endpoint_url=_ENDPOINT, source_name=_SOURCE)

    async def test_bozo_with_no_entries_raises_parse_error(self) -> None:
        feed = _make_feed(entries=[], bozo=True)
        response = _mock_response(text="<not-valid>")

        with (
            _patch_safe_client(response),
            patch(f"{_MOD}.feedparser.parse", return_value=feed),
        ):
            with pytest.raises(FetchParseError):
                await RssReader().fetch(endpoint_url=_ENDPOINT, source_name=_SOURCE)

    async def test_bozo_with_entries_does_not_raise(self) -> None:
        feed = _make_feed(
            entries=[_make_entry(title="ok", link="https://example.com/x")],
            bozo=True,
        )
        response = _mock_response(text="<rss>partial</rss>")

        with (
            _patch_safe_client(response),
            patch(f"{_MOD}.feedparser.parse", return_value=feed),
        ):
            results = await RssReader().fetch(
                endpoint_url=_ENDPOINT, source_name=_SOURCE
            )

        assert len(results) == 1
