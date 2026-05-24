"""``AnthropicSource`` (sitemap.xml, Pattern H) の不変条件テスト。

固定するのは Anthropic Source 固有で他に被覆の無い不変条件:

- fixture sitemap から収集 → 変換経路で永続化 passport が yield
- 収集スコープ ``is_collectable_anthropic_url`` (``/news/`` のみ・対象外 ≠
  変換失敗)。``MAX_ENTRIES`` cap / lastmod 降順
- ``to_fetched_article`` が in-scope entry に対し total (None/raise しない)
- 全 passport は ``ObservedArticle`` (``HTML_TITLE_POLICY``)
- ``RawHttpClient`` の ``ExternalFetchError`` は ``collect`` を素通しする

loc/lastmod parse と XXE 防御は ``SitemapReader`` の責務へ移ったため
``test_sitemap_reader_contract.py`` が SSoT。本ファイルは parse を再検証
しない (旧 ``_parse_sitemap`` 直叩き XXE テストはそこへ relocation)。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.collection.article_acquisition.fetched_article import FetchedArticle
from app.collection.article_acquisition.reader.sitemap_reader import SitemapEntry
from app.collection.article_acquisition.tools.raw_http_client import RawHttpClient
from app.collection.domain.observed_article import ObservedArticle
from app.collection.external_fetch_errors import (
    FetchOriginServerError,
    FetchResourceNotFoundError,
)
from app.collection.sources.definitions.anthropic import (
    AnthropicSource,
    is_collectable_anthropic_url,
    to_fetched_article,
)
from tests.collection.sources._fixture_tools import fixture_tools
from tests.collection.sources._invariant import (
    FetchItem,
    assert_at_least_one_passport,
    assert_passports_persistable,
    drive_source,
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


async def _drive(client: RawHttpClient) -> list[FetchItem]:
    """Anthropic Source を fixture raw client 注入で収集 → 変換経路に通す。

    ``raw`` は ``SitemapReader`` が wrap するため fixture バイトはそのまま
    本物の parse を通る (profile / origin は ``ClassVar`` 直読み)。
    """
    return await drive_source(AnthropicSource, tools=fixture_tools(raw=client))


async def _drive_fixture() -> list[FetchItem]:
    return await _drive(_FakeRawHttpClient(_FIXTURE.read_bytes()))


@pytest.mark.asyncio
async def test_adapter_yields_passports_from_fixture() -> None:
    items = await _drive_fixture()
    assert_at_least_one_passport(items)


@pytest.mark.asyncio
async def test_adapter_persistence_invariants() -> None:
    items = await _drive_fixture()
    assert_passports_persistable(items)


@pytest.mark.asyncio
async def test_only_news_urls_yielded() -> None:
    """``/news/`` 配下の URL のみが yield される (about / pricing 除外)。"""
    items = await _drive_fixture()
    assert items
    for item in items:
        assert isinstance(item, ObservedArticle)
        url = str(item.source_url)
        assert url.startswith("https://www.anthropic.com/news"), url


@pytest.mark.asyncio
async def test_max_entries_capped() -> None:
    items = await _drive_fixture()
    assert len(items) <= AnthropicSource.MAX_ENTRIES


@pytest.mark.asyncio
async def test_all_passports_are_incomplete_for_html_title() -> None:
    """``HTML_TITLE_POLICY`` のため全 passport が ``ObservedArticle``。"""
    items = await _drive_fixture()
    assert items
    for item in items:
        assert isinstance(item, ObservedArticle)


# ── 収集スコープ述語 / 写像 totality (穴1: シームごとに pin) ──────────────


def test_scope_does_not_govern_lastmod() -> None:
    """スコープ述語は lastmod を見ない (lastmod 欠落でも in-scope)。

    これが ``to_fetched_article`` の totality 検証 (下記) の前提。
    """
    entry = SitemapEntry(loc="https://www.anthropic.com/news/x", lastmod=None)
    assert is_collectable_anthropic_url(entry) is True


def test_mapping_is_total_on_in_scope_missing_lastmod() -> None:
    """in-scope だが lastmod 欠落の entry に写像は None/raise せず
    ``FetchedArticle`` を返す (total)。

    converter/fetcher テストは ``FetchedArticle`` を直接与え sitemap 写像を
    通らないため、sitemap シームの totality はここでしか pin できない。
    """
    fa = to_fetched_article(
        SitemapEntry(loc="https://www.anthropic.com/news/x", lastmod=None)
    )
    assert isinstance(fa, FetchedArticle)
    assert fa.url == "https://www.anthropic.com/news/x"
    assert fa.published_at is None


@pytest.mark.asyncio
async def test_non_recoverable_error_propagates_through_collect() -> None:
    client = _RaisingRawHttpClient(
        FetchResourceNotFoundError(status_code=404, reason="not_found")
    )
    with pytest.raises(FetchResourceNotFoundError):
        await _drive(client)


@pytest.mark.asyncio
async def test_recoverable_error_propagates_through_collect() -> None:
    client = _RaisingRawHttpClient(
        FetchOriginServerError(status_code=500, reason="internal_error")
    )
    with pytest.raises(FetchOriginServerError):
        await _drive(client)
