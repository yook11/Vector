"""``HackerNewsFetcher`` の単体テスト (Phase 1e、API Pattern H 唯一のソース)。

per-source 設計:
- Algolia HN Search API、httpx で JSON 取得
- ``url`` 欠落 hit (Ask HN 等) は yield せずに skip (``Failed`` ですらない)
- ``language`` は外部 URL 由来で feed-level に存在せず ``None`` 直書き
  (PROVIDES からも外す)
- ``guid`` は Algolia ``objectID``、``site_name`` は hardcode "Hacker News"
- ``tags`` / ``image_url`` は HN 仕様で提供されないため ``()`` / ``None`` 直書き
- HTTP client は ``make_safe_async_client`` (defense in depth、Pattern H 既存と整合)
- PROVIDES = {guid, site_name}
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.ingestion.domain.fetched_article import (
    Failed,
    PendingHtmlFetch,
)
from app.collection.ingestion.fetchers.hacker_news import (
    HN_HITS_PER_PAGE,
    HN_MIN_POINTS,
    HN_SLIDING_WINDOW_SECONDS,
    HackerNewsFetcher,
)
from app.models.news_source import NewsSource

_HN_MOD = "app.collection.ingestion.fetchers.hacker_news"


def _source(source_id: int = 1, name: str = "Hacker News") -> NewsSource:
    s = MagicMock(spec=NewsSource)
    s.id = source_id
    s.name = name
    s.endpoint_url = "https://hn.algolia.com/api/v1/search_by_date"
    return s


def _hit(**overrides: Any) -> dict[str, Any]:
    """有効な HN hit のベース dict (``url`` 持ち)。"""
    base: dict[str, Any] = {
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
    }
    base.update(overrides)
    return base


def _mock_response(data: dict[str, Any], status_code: int = 200) -> httpx.Response:
    """HN API 向けのモック httpx.Response。"""
    return httpx.Response(
        status_code=status_code,
        text=json.dumps(data),
        headers={"content-type": "application/json"},
        request=httpx.Request("GET", "https://hn.algolia.com/api/v1/search_by_date"),
    )


def _mock_safe_client(response_or_exception: Any) -> MagicMock:
    """``make_safe_async_client`` を mock する async context manager。

    ``response_or_exception`` が Exception なら client.get がそれを raise する。
    """
    client = AsyncMock()
    if isinstance(response_or_exception, BaseException):
        client.get = AsyncMock(side_effect=response_or_exception)
    else:
        client.get = AsyncMock(return_value=response_or_exception)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _request_params(safe_client_cm: MagicMock) -> dict[str, Any]:
    """make_safe_async_client mock から get 呼び出しの params を取り出す。"""
    inner_client: AsyncMock = safe_client_cm.__aenter__.return_value
    return inner_client.get.call_args.kwargs.get("params", {})


class TestProvides:
    def test_provides_minimum_set(self) -> None:
        # language は外部 URL 由来で feed-level に存在しないため含めない
        assert HackerNewsFetcher.PROVIDES == frozenset({"guid", "site_name"})


class TestConvertHit:
    def setup_method(self) -> None:
        self.fetcher = HackerNewsFetcher()
        self.source = _source()

    def test_valid_hit_yields_pending(self) -> None:
        outcome = self.fetcher._convert_hit(_hit(), self.source)
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.title.startswith("I'm helping")

    def test_does_not_construct_body(self) -> None:
        outcome = self.fetcher._convert_hit(_hit(), self.source)
        assert isinstance(outcome, PendingHtmlFetch)
        assert not hasattr(outcome, "body")

    def test_missing_url_returns_none_for_skip(self) -> None:
        # Ask HN 等のテキスト投稿: url=None は yield せずに skip する
        outcome = self.fetcher._convert_hit(_hit(url=None), self.source)
        assert outcome is None

    def test_empty_url_returns_none_for_skip(self) -> None:
        outcome = self.fetcher._convert_hit(_hit(url=""), self.source)
        assert outcome is None

    def test_invalid_url_returns_failed(self) -> None:
        outcome = self.fetcher._convert_hit(_hit(url="not-a-url"), self.source)
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "extraction_empty"

    def test_empty_title_returns_failed(self) -> None:
        outcome = self.fetcher._convert_hit(_hit(title=""), self.source)
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"

    def test_published_at_from_iso8601(self) -> None:
        outcome = self.fetcher._convert_hit(_hit(), self.source)
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.published_at_hint is not None
        assert (
            outcome.published_at_hint.value.tzinfo.utcoffset(None).total_seconds() == 0
        )

    def test_missing_created_at_yields_pending_with_none_hint(self) -> None:
        # Pattern H 緩い品質ゲート: published_at 欠落でも Failed しない
        hit = _hit()
        del hit["created_at"]
        outcome = self.fetcher._convert_hit(hit, self.source)
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.published_at_hint is None

    def test_metadata_author_from_hit(self) -> None:
        outcome = self.fetcher._convert_hit(_hit(), self.source)
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.author == "cleak"

    def test_metadata_tags_empty(self) -> None:
        outcome = self.fetcher._convert_hit(_hit(), self.source)
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.tags == ()

    def test_metadata_image_url_hardcoded_none(self) -> None:
        outcome = self.fetcher._convert_hit(_hit(), self.source)
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.image_url is None

    def test_metadata_guid_is_object_id(self) -> None:
        outcome = self.fetcher._convert_hit(_hit(), self.source)
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.guid == "47139675"

    def test_metadata_language_is_none(self) -> None:
        # PROVIDES 非含有: 外部 URL 由来で feed-level に存在しない
        outcome = self.fetcher._convert_hit(_hit(), self.source)
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.language is None

    def test_metadata_site_name_hardcoded(self) -> None:
        outcome = self.fetcher._convert_hit(_hit(), self.source)
        assert isinstance(outcome, PendingHtmlFetch)
        assert outcome.metadata.site_name == "Hacker News"


_SAMPLE_RESPONSE = {
    "hits": [
        _hit(),
        _hit(objectID="47140000", url=None, title="Ask HN: text post"),
        _hit(
            objectID="47140001",
            title="New Rust release v2.0",
            url="https://blog.rust-lang.org/2026/02/24/rust-2.html",
            author="rustdev",
        ),
    ],
    "nbHits": 3,
    "page": 0,
    "nbPages": 1,
    "hitsPerPage": 100,
}


@pytest.mark.asyncio
class TestFetch:
    async def _collect(self, fetcher: HackerNewsFetcher, source: NewsSource) -> list:
        outcomes: list = []
        async for outcome in fetcher.fetch(source):
            outcomes.append(outcome)
        return outcomes

    async def test_fetch_yields_pending_for_valid_hits(self) -> None:
        cm = _mock_safe_client(_mock_response(_SAMPLE_RESPONSE))
        with patch(f"{_HN_MOD}.make_safe_async_client", return_value=cm):
            outcomes = await self._collect(HackerNewsFetcher(), _source())

        # url=None の Ask HN は skip、残り 2 件が PendingHtmlFetch
        assert len(outcomes) == 2
        assert all(isinstance(o, PendingHtmlFetch) for o in outcomes)
        assert outcomes[0].metadata.guid == "47139675"
        assert outcomes[1].metadata.guid == "47140001"

    async def test_fetch_uses_sliding_window_param(self) -> None:
        cm = _mock_safe_client(_mock_response({"hits": []}))
        fixed_now = 1_800_000_000
        with (
            patch(f"{_HN_MOD}.make_safe_async_client", return_value=cm),
            patch(f"{_HN_MOD}.time.time", return_value=fixed_now),
        ):
            await self._collect(HackerNewsFetcher(), _source())

        expected_since = fixed_now - HN_SLIDING_WINDOW_SECONDS
        numeric_filters = _request_params(cm).get("numericFilters", "")
        assert f"created_at_i>{expected_since}" in numeric_filters

    async def test_fetch_uses_min_points_param(self) -> None:
        cm = _mock_safe_client(_mock_response({"hits": []}))
        with patch(f"{_HN_MOD}.make_safe_async_client", return_value=cm):
            await self._collect(HackerNewsFetcher(), _source())

        numeric_filters = _request_params(cm).get("numericFilters", "")
        assert f"points>{HN_MIN_POINTS}" in numeric_filters

    async def test_fetch_uses_hits_per_page_and_tags(self) -> None:
        cm = _mock_safe_client(_mock_response({"hits": []}))
        with patch(f"{_HN_MOD}.make_safe_async_client", return_value=cm):
            await self._collect(HackerNewsFetcher(), _source())

        params = _request_params(cm)
        assert params["hitsPerPage"] == HN_HITS_PER_PAGE
        assert params["tags"] == "story"

    async def test_fetch_403_raises_permanent(self) -> None:
        response = _mock_response({}, status_code=403)
        error = httpx.HTTPStatusError(
            "403", request=response.request, response=response
        )
        cm = _mock_safe_client(error)
        with (
            patch(f"{_HN_MOD}.make_safe_async_client", return_value=cm),
            pytest.raises(PermanentFetchError),
        ):
            await self._collect(HackerNewsFetcher(), _source())

    async def test_fetch_500_raises_temporary(self) -> None:
        response = _mock_response({}, status_code=500)
        error = httpx.HTTPStatusError(
            "500", request=response.request, response=response
        )
        cm = _mock_safe_client(error)
        with (
            patch(f"{_HN_MOD}.make_safe_async_client", return_value=cm),
            pytest.raises(TemporaryFetchError),
        ):
            await self._collect(HackerNewsFetcher(), _source())

    async def test_fetch_request_error_raises_temporary(self) -> None:
        error = httpx.RequestError("connection failed")
        cm = _mock_safe_client(error)
        with (
            patch(f"{_HN_MOD}.make_safe_async_client", return_value=cm),
            pytest.raises(TemporaryFetchError),
        ):
            await self._collect(HackerNewsFetcher(), _source())

    async def test_fetch_returns_empty_when_no_hits(self) -> None:
        cm = _mock_safe_client(_mock_response({"hits": []}))
        with patch(f"{_HN_MOD}.make_safe_async_client", return_value=cm):
            outcomes = await self._collect(HackerNewsFetcher(), _source())
        assert outcomes == []
