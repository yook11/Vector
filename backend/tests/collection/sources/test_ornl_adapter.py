"""``ORNLSource`` (HTML listing, Pattern H) の不変条件テスト。

固定するのは ORNL Source 固有で他に被覆の無い不変条件:

- fixture HTML listing から収集 → 変換経路で永続化 passport が yield
- 収集スコープ ``is_collectable_ornl_url`` (``EXCLUDED_PATHS`` 除外・対象外 ≠
  変換失敗)。同 listing 内 URL dedup / ``MAX_ENTRIES`` cap
- ``to_fetched_article`` が in-scope entry に対し total (None/raise しない)
- 全 passport は ``ObservedArticle`` / ``published_at=None``
- ``RawHttpClient`` の ``ExternalFetchError`` は ``collect`` を素通しする

href 抽出と xpath 契約は ``HtmlListingReader`` の責務へ移ったため
``test_html_listing_reader_contract.py`` が SSoT。本ファイルは抽出を再検証
しない (旧 ``_parse_listing`` 直叩き xpath テストはそこへ relocation)。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.collection.article_collection.fetched_article import FetchedArticle
from app.collection.article_collection.reader.html_listing_reader import (
    HtmlListingEntry,
)
from app.collection.article_collection.tools.raw_http_client import RawHttpClient
from app.collection.domain.observed_article import ObservedArticle
from app.collection.external_fetch_errors import (
    FetchOriginServerError,
    FetchResourceNotFoundError,
)
from app.collection.sources.definitions.ornl import (
    ORNLSource,
    is_collectable_ornl_url,
    to_fetched_article,
)
from tests.collection.sources._fixture_tools import fixture_tools
from tests.collection.sources._invariant import (
    FetchItem,
    assert_at_least_one_passport,
    assert_passports_persistable,
    drive_source,
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


async def _drive(client: RawHttpClient) -> list[FetchItem]:
    """ORNL Source を fixture raw client 注入で収集 → 変換経路に通す。

    ``raw`` は ``HtmlListingReader`` が wrap するため fixture バイトはそのまま
    本物の parse を通る (profile / origin は ``ClassVar`` 直読み)。
    """
    return await drive_source(ORNLSource, tools=fixture_tools(raw=client))


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
async def test_category_landings_dropped() -> None:
    """``EXCLUDED_PATHS`` 配下の URL は yield されない (収集スコープ外)。"""
    items = await _drive_fixture()
    yielded_paths = {
        "/" + str(item.source_url).split("/", 3)[3].rstrip("/")
        for item in items
        if isinstance(item, ObservedArticle)
    }
    for excluded in ORNLSource.EXCLUDED_PATHS:
        assert excluded not in yielded_paths


@pytest.mark.asyncio
async def test_listing_internal_dedup_applied() -> None:
    """同 listing で同 URL が複数 ``<a>`` で出ても重複 yield されない。"""
    items = await _drive_fixture()
    urls = [str(item.source_url) for item in items]
    assert len(urls) == len(set(urls))


@pytest.mark.asyncio
async def test_max_entries_capped() -> None:
    items = await _drive_fixture()
    assert len(items) <= ORNLSource.MAX_ENTRIES


@pytest.mark.asyncio
async def test_all_passports_are_incomplete() -> None:
    items = await _drive_fixture()
    assert items
    for item in items:
        assert isinstance(item, ObservedArticle)
        assert item.published_at is None


# ── 収集スコープ述語 / 写像 totality (穴1: シームごとに pin) ──────────────


def test_scope_does_not_govern_slug() -> None:
    """スコープ述語は slug を見ない (slug 空でも in-scope)。

    これが ``to_fetched_article`` の totality 検証 (下記) の前提。
    """
    assert is_collectable_ornl_url(HtmlListingEntry(href="/news/x")) is True


def test_mapping_is_total_on_in_scope_entry() -> None:
    """in-scope entry に写像は None/raise せず ``FetchedArticle`` を返す
    (total)。相対 href → 絶対 URL は Source 純写像。

    converter/fetcher テストは ``FetchedArticle`` を直接与え listing 写像を
    通らないため、listing シームの totality はここでしか pin できない。
    """
    fa = to_fetched_article(HtmlListingEntry(href="/news/x"))
    assert isinstance(fa, FetchedArticle)
    assert fa.url == "https://www.ornl.gov/news/x"
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
