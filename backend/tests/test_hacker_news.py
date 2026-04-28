"""Hacker News フェッチャーのテスト。

Fetcher の責務は "外部 API → ArticleCandidate dict" の変換に限定されるため、
DB 永続化や重複排除に関するテストは ``test_source_fetch_service.py`` 側で行う。
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.ingestion.fetchers.hacker_news import (
    HN_HITS_PER_PAGE,
    HN_MIN_POINTS,
    HN_SLIDING_WINDOW_SECONDS,
    HackerNewsFetcher,
    HNStory,
)

_HN_MOD = "app.collection.ingestion.fetchers.hacker_news"

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


def _sample_hn_source() -> MagicMock:
    """DB 非依存の HN NewsSource ダミー。"""
    source = MagicMock()
    source.id = 1
    source.name = "Hacker News"
    source.endpoint_url = "https://hn.algolia.com/api/v1/search_by_date"
    return source


def _request_params(mock_client: AsyncMock) -> dict:
    """mock_http_client.get の呼び出しから params を取り出す。"""
    return mock_client.get.call_args.kwargs.get("params", {})


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


async def test_fetch_recent_stories_uses_sliding_window(
    mock_http_client: AsyncMock,
) -> None:
    """毎サイクル numericFilters に created_at_i>{now-86400} を含める。"""
    mock_http_client.get.return_value = _mock_hn_response(data={"hits": []})

    fixed_now = 1_800_000_000
    with patch(f"{_HN_MOD}.time.time", return_value=fixed_now):
        fetcher = HackerNewsFetcher()
        await fetcher._fetch_recent_stories(mock_http_client)

    expected_since = fixed_now - HN_SLIDING_WINDOW_SECONDS
    numeric_filters = _request_params(mock_http_client).get("numericFilters", "")
    assert f"created_at_i>{expected_since}" in numeric_filters


async def test_fetch_recent_stories_includes_min_points_filter(
    mock_http_client: AsyncMock,
) -> None:
    """numericFilters に points>{HN_MIN_POINTS} を含める。Algolia 側で除外させる。"""
    mock_http_client.get.return_value = _mock_hn_response(data={"hits": []})

    fetcher = HackerNewsFetcher()
    await fetcher._fetch_recent_stories(mock_http_client)

    numeric_filters = _request_params(mock_http_client).get("numericFilters", "")
    assert f"points>{HN_MIN_POINTS}" in numeric_filters


async def test_fetch_recent_stories_uses_configured_hits_per_page(
    mock_http_client: AsyncMock,
) -> None:
    """params に hitsPerPage=HN_HITS_PER_PAGE を指定する。"""
    mock_http_client.get.return_value = _mock_hn_response(data={"hits": []})

    fetcher = HackerNewsFetcher()
    await fetcher._fetch_recent_stories(mock_http_client)

    assert _request_params(mock_http_client).get("hitsPerPage") == HN_HITS_PER_PAGE


async def test_fetch_recent_stories_api_error(
    mock_http_client: AsyncMock,
) -> None:
    """HTTP エラーは HTTPStatusError として伝播する。"""
    mock_http_client.get.return_value = _mock_hn_response(data={}, status_code=429)

    fetcher = HackerNewsFetcher()
    with pytest.raises(httpx.HTTPStatusError):
        await fetcher._fetch_recent_stories(mock_http_client)


# --- HackerNewsFetcher.fetch tests ---


async def test_fetch_returns_candidates_for_stories(
    mock_http_client: AsyncMock,
) -> None:
    """HN story が ArticleCandidate の dict に変換される。"""
    mock_http_client.get.return_value = _mock_hn_response()

    fetcher = HackerNewsFetcher()
    candidates = await fetcher.fetch(
        client=mock_http_client, source=_sample_hn_source()
    )

    assert len(candidates) == 2
    titles = {c.title for c in candidates.values()}
    assert "I'm helping my dog vibe code games" in titles
    assert "New Rust release v2.0" in titles


async def test_fetch_returns_same_story_on_repeated_call(
    mock_http_client: AsyncMock,
) -> None:
    """同一 story が連続 fetch で毎回返ること。

    sliding window 設計の核: slow-maturing story が 24h 以内に points を
    伸ばしている間、毎サイクル candidates に含めて返し、dedup は repository
    層 (ON CONFLICT DO NOTHING) に委ねる。
    """
    mock_http_client.get.return_value = _mock_hn_response()

    fetcher = HackerNewsFetcher()
    first = await fetcher.fetch(client=mock_http_client, source=_sample_hn_source())
    second = await fetcher.fetch(client=mock_http_client, source=_sample_hn_source())

    assert set(first.keys()) == set(second.keys())
    assert len(first) == 2


async def test_fetch_temporary_error_on_5xx(
    mock_http_client: AsyncMock,
) -> None:
    """5xx は TemporaryFetchError を raise する。"""
    mock_http_client.get.return_value = _mock_hn_response(data={}, status_code=500)

    fetcher = HackerNewsFetcher()
    with pytest.raises(TemporaryFetchError):
        await fetcher.fetch(client=mock_http_client, source=_sample_hn_source())


async def test_fetch_permanent_error_on_404(
    mock_http_client: AsyncMock,
) -> None:
    """404 は PermanentFetchError を raise する。"""
    mock_http_client.get.return_value = _mock_hn_response(data={}, status_code=404)

    fetcher = HackerNewsFetcher()
    with pytest.raises(PermanentFetchError):
        await fetcher.fetch(client=mock_http_client, source=_sample_hn_source())


async def test_fetch_temporary_error_on_network_failure(
    mock_http_client: AsyncMock,
) -> None:
    """ネットワークエラーは TemporaryFetchError を raise する。"""
    mock_http_client.get.side_effect = httpx.ConnectError("Connection refused")

    fetcher = HackerNewsFetcher()
    with pytest.raises(TemporaryFetchError):
        await fetcher.fetch(client=mock_http_client, source=_sample_hn_source())


async def test_fetch_empty_response(
    mock_http_client: AsyncMock,
) -> None:
    """API レスポンスが空なら空 dict を返す。"""
    mock_http_client.get.return_value = _mock_hn_response(data={"hits": []})

    fetcher = HackerNewsFetcher()
    candidates = await fetcher.fetch(
        client=mock_http_client, source=_sample_hn_source()
    )

    assert candidates == {}
