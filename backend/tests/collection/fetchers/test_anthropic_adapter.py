"""``AnthropicAdapter`` (sitemap.xml, Pattern H) の不変条件テスト。

検証する不変条件:

- fixture sitemap.xml から ``ArticleFetcher`` 経由で永続化 passport が yield される
- ``URL_PATH_PREFIX="/news/"`` 以外の URL は yield されない
- ``MAX_ENTRIES=30`` で切り出される
- 各 passport は ``prefer_html_title=True`` 経由で ``IncompleteArticle`` 型
- ``RawHttpClient`` の ``ExternalFetchError`` は Adapter を素通しする
- sitemap parser は XXE / 外部 entity を解決しない (defensive parsing 契約)
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
from app.collection.fetchers.anthropic import AnthropicAdapter, _parse_sitemap
from app.collection.fetchers.article_fetcher import ArticleFetcher
from app.collection.fetchers.tools.raw_http_client import RawHttpClient
from tests.collection.fetchers._invariant import (
    Passport,
    assert_at_least_one_passport,
    assert_passports_persistable,
)

_FIXTURE = Path(__file__).parent.parent.parent / "fixtures" / "anthropic_sitemap.xml"


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


def _build_adapter() -> AnthropicAdapter:
    return AnthropicAdapter(client=_FakeRawHttpClient(_FIXTURE.read_bytes()))


@pytest.mark.asyncio
async def test_adapter_yields_passports_from_fixture() -> None:
    items = await _collect(ArticleFetcher(_build_adapter()).fetch(source_id=1))
    assert_at_least_one_passport(items)


@pytest.mark.asyncio
async def test_adapter_persistence_invariants() -> None:
    items = await _collect(ArticleFetcher(_build_adapter()).fetch(source_id=1))
    assert_passports_persistable(items)


@pytest.mark.asyncio
async def test_only_news_urls_yielded() -> None:
    """``/news/`` 配下の URL のみが yield される (about / pricing 除外)。

    URL canonical で trailing slash が剥がれるため、``/news`` (index) と
    ``/news/<slug>`` (記事) のどちらも許容するが、``/news`` 以外で始まる
    path は yield されないことを固定する。
    """
    items = await _collect(ArticleFetcher(_build_adapter()).fetch(source_id=1))
    assert items
    for item in items:
        assert isinstance(item, IncompleteArticle)
        url = str(item.source_url)
        assert url.startswith("https://www.anthropic.com/news"), url


@pytest.mark.asyncio
async def test_max_entries_capped() -> None:
    items = await _collect(ArticleFetcher(_build_adapter()).fetch(source_id=1))
    assert len(items) <= AnthropicAdapter.MAX_ENTRIES


@pytest.mark.asyncio
async def test_all_passports_are_incomplete_for_html_title() -> None:
    """``prefer_html_title=True`` 経由のため Ready 経路は発火しない。"""
    items = await _collect(ArticleFetcher(_build_adapter()).fetch(source_id=1))
    assert items
    for item in items:
        assert isinstance(item, IncompleteArticle)


@pytest.mark.asyncio
async def test_non_recoverable_error_propagates_through_adapter() -> None:
    adapter = AnthropicAdapter(
        client=_RaisingRawHttpClient(
            FetchResourceNotFoundError(status_code=404, reason="not_found")
        )
    )
    with pytest.raises(FetchResourceNotFoundError):
        await _collect(ArticleFetcher(adapter).fetch(source_id=1))


@pytest.mark.asyncio
async def test_recoverable_error_propagates_through_adapter() -> None:
    adapter = AnthropicAdapter(
        client=_RaisingRawHttpClient(
            FetchOriginServerError(status_code=500, reason="internal_error")
        )
    )
    with pytest.raises(FetchOriginServerError):
        await _collect(ArticleFetcher(adapter).fetch(source_id=1))


def test_xxe_external_entity_disabled() -> None:
    """sitemap parser は外部実体参照を解決しない (defensive parsing 契約)。"""
    malicious = b"""<?xml version="1.0"?>
<!DOCTYPE urlset [
  <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.anthropic.com/news/&xxe;</loc></url>
</urlset>
"""
    entries = _parse_sitemap(malicious)
    loc = entries[0][0] if entries else ""
    assert "/etc/passwd" not in loc
    assert "root:" not in loc
