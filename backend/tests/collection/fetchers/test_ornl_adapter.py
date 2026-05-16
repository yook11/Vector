"""``ORNLAdapter`` (HTML listing, Pattern H) の不変条件テスト。

検証する不変条件:

- fixture HTML listing から ``ArticleFetcher`` 経由で永続化 passport が yield される
- ``EXCLUDED_PATHS`` (category landing) は yield されない
- 同一 listing 内で同 URL が複数回 ``<a>`` から検出されても dedup される
- ``MAX_ENTRIES=30`` で切り出される
- 全 passport は ``IncompleteArticle`` (``prefer_html_title=True``)
- ``published_at_hint=None`` (listing には lastmod 情報がない前提)
- ``RawHttpClient`` の ``ExternalFetchError`` は Adapter を素通しする
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.collection.domain.incomplete_article import (
    IncompleteArticle,
)
from app.collection.external_fetch_errors import (
    FetchOriginServerError,
    FetchResourceNotFoundError,
)
from app.collection.fetchers.article_fetcher import ArticleFetcher
from app.collection.fetchers.ornl import ORNLAdapter, _parse_listing
from app.collection.fetchers.tools.raw_http_client import RawHttpClient
from tests.collection.fetchers._invariant import (
    Passport,
    assert_at_least_one_passport,
    assert_passports_persistable,
)

_FIXTURE = Path(__file__).parent.parent.parent / "fixtures" / "ornl_listing.html"


class _FakeRawHttpClient(RawHttpClient):
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def fetch(self, *, url: str, source_name: str) -> bytes:  # noqa: ARG002
        return self._payload


class _RaisingRawHttpClient(RawHttpClient):
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def fetch(self, *, url: str, source_name: str) -> bytes:  # noqa: ARG002
        raise self._exc


async def _collect(it: AsyncIterator[Passport]) -> list[Passport]:
    return [o async for o in it]


def _build_adapter() -> ORNLAdapter:
    return ORNLAdapter(client=_FakeRawHttpClient(_FIXTURE.read_bytes()))


@pytest.mark.asyncio
async def test_adapter_yields_passports_from_fixture() -> None:
    items = await _collect(ArticleFetcher(_build_adapter()).fetch(source_id=1))
    assert_at_least_one_passport(items)


@pytest.mark.asyncio
async def test_adapter_persistence_invariants() -> None:
    items = await _collect(ArticleFetcher(_build_adapter()).fetch(source_id=1))
    assert_passports_persistable(items)


@pytest.mark.asyncio
async def test_category_landings_dropped() -> None:
    """``EXCLUDED_PATHS`` 配下の URL は yield されない。"""
    items = await _collect(ArticleFetcher(_build_adapter()).fetch(source_id=1))
    yielded_paths = {
        "/" + str(item.source_url).split("/", 3)[3].rstrip("/")
        for item in items
        if isinstance(item, IncompleteArticle)
    }
    for excluded in ORNLAdapter.EXCLUDED_PATHS:
        assert excluded not in yielded_paths


@pytest.mark.asyncio
async def test_listing_internal_dedup_applied() -> None:
    """同 listing で同 URL が複数 ``<a>`` で出ても重複 yield されない。"""
    items = await _collect(ArticleFetcher(_build_adapter()).fetch(source_id=1))
    urls = [str(item.source_url) for item in items]
    assert len(urls) == len(set(urls))


@pytest.mark.asyncio
async def test_max_entries_capped() -> None:
    items = await _collect(ArticleFetcher(_build_adapter()).fetch(source_id=1))
    assert len(items) <= ORNLAdapter.MAX_ENTRIES


@pytest.mark.asyncio
async def test_all_passports_are_incomplete() -> None:
    items = await _collect(ArticleFetcher(_build_adapter()).fetch(source_id=1))
    assert items
    for item in items:
        assert isinstance(item, IncompleteArticle)
        assert item.published_at_hint is None


@pytest.mark.asyncio
async def test_non_recoverable_error_propagates_through_adapter() -> None:
    adapter = ORNLAdapter(
        client=_RaisingRawHttpClient(
            FetchResourceNotFoundError(status_code=404, reason="not_found")
        )
    )
    with pytest.raises(FetchResourceNotFoundError):
        await _collect(ArticleFetcher(adapter).fetch(source_id=1))


@pytest.mark.asyncio
async def test_recoverable_error_propagates_through_adapter() -> None:
    adapter = ORNLAdapter(
        client=_RaisingRawHttpClient(
            FetchOriginServerError(status_code=500, reason="internal_error")
        )
    )
    with pytest.raises(FetchOriginServerError):
        await _collect(ArticleFetcher(adapter).fetch(source_id=1))


def test_listing_xpath_extracts_only_news_links() -> None:
    urls = _parse_listing(
        _FIXTURE.read_bytes(),
        detail_link_xpath=ORNLAdapter.DETAIL_LINK_XPATH,
        detail_url_prefix=ORNLAdapter.DETAIL_URL_PREFIX,
    )
    assert urls
    assert all(url.startswith("https://www.ornl.gov/news/") for url in urls)
