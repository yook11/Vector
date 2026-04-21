"""Hacker News フェッチャーのテスト。

Fetcher の責務は "外部 API → ArticleCandidate dict" の変換に限定されるため、
DB 永続化や重複排除に関するテストは ``test_source_fetch_service.py`` 側で行う。
"""

from collections.abc import Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.ingestion.fetchers.hacker_news import HackerNewsFetcher, HNStory

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


@pytest.fixture(autouse=True)
def mock_hn_fetch_state() -> Generator[dict[str, AsyncMock], None, None]:
    """HN 増分取得 state (Redis) を全テストでデフォルト mock する。"""
    with (
        patch(
            f"{_HN_MOD}.get_last_fetched_at",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_get,
        patch(
            f"{_HN_MOD}.set_last_fetched_at",
            new_callable=AsyncMock,
        ) as mock_set,
    ):
        yield {"get": mock_get, "set": mock_set}


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


async def test_fetch_uses_last_fetched_at_from_redis(
    mock_http_client: AsyncMock,
    mock_hn_fetch_state: dict[str, AsyncMock],
) -> None:
    """Redis に保存された last_fetched_at が API フィルタに使われる。"""
    mock_hn_fetch_state["get"].return_value = datetime(
        2026, 2, 24, 17, 0, 0, tzinfo=UTC
    )
    mock_http_client.get.return_value = _mock_hn_response(data={"hits": []})

    fetcher = HackerNewsFetcher()
    await fetcher.fetch(client=mock_http_client, source=_sample_hn_source())

    call_kwargs = mock_http_client.get.call_args
    params = call_kwargs.kwargs.get("params", {})
    numeric_filters = params.get("numericFilters", "")
    assert "created_at_i>" in numeric_filters


async def test_fetch_updates_last_fetched_at_on_success(
    mock_http_client: AsyncMock,
    mock_hn_fetch_state: dict[str, AsyncMock],
) -> None:
    """成功時は Redis の last_fetched_at を現在時刻で更新する。"""
    mock_http_client.get.return_value = _mock_hn_response(data={"hits": []})
    source = _sample_hn_source()

    fetcher = HackerNewsFetcher()
    await fetcher.fetch(client=mock_http_client, source=source)

    mock_hn_fetch_state["set"].assert_awaited_once()
    args, _ = mock_hn_fetch_state["set"].call_args
    assert args[0] == source.id
    assert isinstance(args[1], datetime)


async def test_fetch_does_not_update_state_on_error(
    mock_http_client: AsyncMock,
    mock_hn_fetch_state: dict[str, AsyncMock],
) -> None:
    """fetch 失敗時は last_fetched_at を更新しない。"""
    mock_http_client.get.return_value = _mock_hn_response(data={}, status_code=500)

    fetcher = HackerNewsFetcher()
    with pytest.raises(TemporaryFetchError):
        await fetcher.fetch(client=mock_http_client, source=_sample_hn_source())

    mock_hn_fetch_state["set"].assert_not_called()


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
