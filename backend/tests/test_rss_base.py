"""BaseRssFetcher のテスト。

Stub サブクラスを使い、基底クラスの共通フロー + デフォルト convert_entry をテストする。
ユーティリティ関数のユニットテストも含む。

Fetcher の責務は "外部配信 → ArticleCandidate dict" の変換に限定されるため、
DB 永続化に関するテストは ``test_source_fetch_service.py`` 側で行う。
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.ingestion.fetchers.rss.base import BaseRssFetcher, extract_guid

_BASE_MOD = "app.collection.ingestion.fetchers.rss.base"


class StubRssFetcher(BaseRssFetcher):
    """テスト用。デフォルト convert_entry を継承。"""


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
    return AsyncMock(spec=httpx.AsyncClient)


def _sample_source() -> MagicMock:
    """テスト用 NewsSource ダミー (DB 非依存)。"""
    source = MagicMock()
    source.id = 1
    source.name = "Test Tech Source"
    source.endpoint_url = "https://example.com/feed.xml"
    return source


# --- ユーティリティ関数のユニットテスト ---


def test_extract_guid_prefers_id() -> None:
    entry = {"id": "urn:uuid:12345", "link": "https://example.com/article"}
    assert extract_guid(entry) == "urn:uuid:12345"


def test_extract_guid_falls_back_to_link() -> None:
    entry = {"link": "https://example.com/article"}
    assert extract_guid(entry) == "https://example.com/article"


def test_extract_guid_returns_none_for_empty() -> None:
    assert extract_guid({}) is None


# --- 共通フローのユニットテスト（DB なし） ---


async def test_rss_returns_candidates_for_entries(mock_client: AsyncMock) -> None:
    """feed エントリが ArticleCandidate の dict に変換される。"""
    entries = [
        _make_entry(title="Article 1", link="https://example.com/1"),
        _make_entry(title="Article 2", link="https://example.com/2"),
    ]
    feed = _make_feed(entries)
    mock_client.get.return_value = _mock_response(text="<rss>mock</rss>")

    with (
        patch(f"{_BASE_MOD}.feedparser.parse", return_value=feed),
        patch(
            f"{_BASE_MOD}.get_http_cache",
            new_callable=AsyncMock,
            return_value=(None, None),
        ),
        patch(f"{_BASE_MOD}.set_http_cache", new_callable=AsyncMock),
    ):
        candidates = await StubRssFetcher().fetch(mock_client, _sample_source())

    assert len(candidates) == 2
    titles = {c.title for c in candidates.values()}
    assert titles == {"Article 1", "Article 2"}


async def test_rss_deduplicates_same_url_within_feed(mock_client: AsyncMock) -> None:
    """同一 URL のエントリは先勝ちで dict キーが一意化される。"""
    entries = [
        _make_entry(title="First", link="https://example.com/dup"),
        _make_entry(title="Second", link="https://example.com/dup"),
    ]
    feed = _make_feed(entries)
    mock_client.get.return_value = _mock_response(text="<rss>mock</rss>")

    with (
        patch(f"{_BASE_MOD}.feedparser.parse", return_value=feed),
        patch(
            f"{_BASE_MOD}.get_http_cache",
            new_callable=AsyncMock,
            return_value=(None, None),
        ),
        patch(f"{_BASE_MOD}.set_http_cache", new_callable=AsyncMock),
    ):
        candidates = await StubRssFetcher().fetch(mock_client, _sample_source())

    assert len(candidates) == 1
    assert next(iter(candidates.values())).title == "First"


async def test_rss_handles_304_not_modified(mock_client: AsyncMock) -> None:
    """304 は例外ではなく空 dict として返される。"""
    mock_client.get.return_value = _mock_response(status_code=304)

    with patch(
        f"{_BASE_MOD}.get_http_cache",
        new_callable=AsyncMock,
        return_value=(None, None),
    ):
        candidates = await StubRssFetcher().fetch(mock_client, _sample_source())

    assert candidates == {}


async def test_rss_temporary_error_on_5xx(mock_client: AsyncMock) -> None:
    """5xx は TemporaryFetchError を raise する。"""
    mock_client.get.return_value = _mock_response(status_code=500)

    with patch(
        f"{_BASE_MOD}.get_http_cache",
        new_callable=AsyncMock,
        return_value=(None, None),
    ):
        with pytest.raises(TemporaryFetchError):
            await StubRssFetcher().fetch(mock_client, _sample_source())


async def test_rss_permanent_error_on_404(mock_client: AsyncMock) -> None:
    """404 は PermanentFetchError を raise する。"""
    mock_client.get.return_value = _mock_response(status_code=404)

    with patch(
        f"{_BASE_MOD}.get_http_cache",
        new_callable=AsyncMock,
        return_value=(None, None),
    ):
        with pytest.raises(PermanentFetchError):
            await StubRssFetcher().fetch(mock_client, _sample_source())


async def test_rss_temporary_error_on_network_failure(mock_client: AsyncMock) -> None:
    """接続エラーは TemporaryFetchError を raise する。"""
    mock_client.get.side_effect = httpx.ConnectError("connection refused")

    with patch(
        f"{_BASE_MOD}.get_http_cache",
        new_callable=AsyncMock,
        return_value=(None, None),
    ):
        with pytest.raises(TemporaryFetchError):
            await StubRssFetcher().fetch(mock_client, _sample_source())


async def test_rss_permanent_error_on_bozo_feed(mock_client: AsyncMock) -> None:
    """feedparser bozo でエントリが空なら PermanentFetchError を raise する。"""
    feed = _make_feed(entries=[], bozo=True)
    mock_client.get.return_value = _mock_response(text="<not-valid>")

    with (
        patch(f"{_BASE_MOD}.feedparser.parse", return_value=feed),
        patch(
            f"{_BASE_MOD}.get_http_cache",
            new_callable=AsyncMock,
            return_value=(None, None),
        ),
        patch(f"{_BASE_MOD}.set_http_cache", new_callable=AsyncMock),
    ):
        with pytest.raises(PermanentFetchError):
            await StubRssFetcher().fetch(mock_client, _sample_source())


async def test_rss_sends_conditional_get_headers(mock_client: AsyncMock) -> None:
    """ETag と Last-Modified は Redis から読み出しヘッダーとして送信する。"""
    mock_client.get.return_value = _mock_response(status_code=304)

    with patch(
        f"{_BASE_MOD}.get_http_cache",
        new_callable=AsyncMock,
        return_value=('"abc123"', "Wed, 01 Jan 2025 00:00:00 GMT"),
    ):
        await StubRssFetcher().fetch(mock_client, _sample_source())

    call_kwargs = mock_client.get.call_args
    headers = call_kwargs.kwargs.get("headers", {})
    assert headers["If-None-Match"] == '"abc123"'
    assert headers["If-Modified-Since"] == "Wed, 01 Jan 2025 00:00:00 GMT"


async def test_rss_captures_etag_and_writes_to_redis(mock_client: AsyncMock) -> None:
    """レスポンスの ETag と Last-Modified は Redis に書き込む。"""
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
        patch(f"{_BASE_MOD}.feedparser.parse", return_value=feed),
        patch(
            f"{_BASE_MOD}.get_http_cache",
            new_callable=AsyncMock,
            return_value=(None, None),
        ),
        patch(
            f"{_BASE_MOD}.set_http_cache",
            new_callable=AsyncMock,
        ) as mock_set_cache,
    ):
        await StubRssFetcher().fetch(mock_client, source)

    mock_set_cache.assert_called_once_with(
        source.id, '"new-etag"', "Thu, 02 Jan 2025 00:00:00 GMT"
    )
