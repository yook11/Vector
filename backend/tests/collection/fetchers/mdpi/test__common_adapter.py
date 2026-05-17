"""``mdpi_items`` 共通処理 (Crossref API, Pattern R) の不変条件テスト (P2-D)。

P2-D で MDPI 4 journal は ``mdpi_items`` 共通処理を共有する独立 Source クラス
(``mdpi/sources.py``) になった。本テストは ``MDPIMaterialsSource`` を代表に
共通処理の判定契約を pin する。

検証する不変条件:

- fixture items から ``ArticleFetcher`` 経由で永続化 passport が yield される
- ``type != "journal-article"`` / non-CC-BY-4 license / 必須フィールド欠落の
  各 drop branch が yield 自体されない (passport にならない)
- 全 passport が ``AnalyzableArticle`` で ``source_url = https://doi.org/<DOI>``
- ``CrossrefApiClient`` の ``ExternalFetchError`` は ``collect`` を素通しする
- ``works()`` には注入 ``issn`` / ``from_pub_date`` / ``rows`` (既定 20) が渡る
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.external_fetch_errors import (
    FetchAccessDeniedError,
    FetchOriginServerError,
)
from app.collection.fetchers.article_fetcher import ArticleFetcher
from app.collection.fetchers.mdpi.sources import MDPIMaterialsSource
from app.collection.fetchers.tools.crossref_client import CrossrefApiClient
from tests.collection.fetchers._fixture_tools import fixture_tools
from tests.collection.fetchers._invariant import (
    Passport,
    assert_at_least_one_passport,
    assert_passports_persistable,
)

_FIXTURE = (
    Path(__file__).parent.parent.parent.parent / "fixtures" / "mdpi_crossref.json"
)
_MATERIALS_ISSN = "1996-1944"
_DEFAULT_ROWS_PER_REQUEST = 20


def _items() -> list[dict[str, Any]]:
    raw = json.loads(_FIXTURE.read_text())
    return list(raw["message"]["items"])


def _valid_item() -> dict[str, Any]:
    return deepcopy(_items()[0])


class _FakeCrossrefClient(CrossrefApiClient):
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self._items = items
        self.calls: list[dict[str, Any]] = []

    async def works(
        self,
        *,
        source_name: str,
        issn: str,
        from_pub_date: str,
        rows: int,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            {
                "source_name": source_name,
                "issn": issn,
                "from_pub_date": from_pub_date,
                "rows": rows,
            }
        )
        return self._items


class _RaisingCrossrefClient(CrossrefApiClient):
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def works(
        self,
        *,
        source_name: str,  # noqa: ARG002
        issn: str,  # noqa: ARG002
        from_pub_date: str,  # noqa: ARG002
        rows: int,  # noqa: ARG002
    ) -> list[dict[str, Any]]:
        raise self._exc


async def _collect(it: AsyncIterator[Passport]) -> list[Passport]:
    return [o async for o in it]


def _fetcher(client: CrossrefApiClient) -> ArticleFetcher:
    """``MDPIMaterialsSource`` を fixture client 注入で ``ArticleFetcher`` 化。"""
    return ArticleFetcher(MDPIMaterialsSource, tools=fixture_tools(crossref=client))


def _build(items: list[dict[str, Any]]) -> ArticleFetcher:
    return _fetcher(_FakeCrossrefClient(items))


@pytest.mark.asyncio
async def test_adapter_yields_passports_from_fixture() -> None:
    items = await _collect(_build(_items()).fetch(source_id=1))
    assert_at_least_one_passport(items)


@pytest.mark.asyncio
async def test_adapter_persistence_invariants() -> None:
    items = await _collect(_build(_items()).fetch(source_id=1))
    assert_passports_persistable(items)


@pytest.mark.asyncio
async def test_only_one_valid_item_yields_passport() -> None:
    """fixture 3 records: valid / correction (drop) / no-license (drop) → 1 件のみ。"""
    items = await _collect(_build(_items()).fetch(source_id=1))
    passports = [o for o in items if isinstance(o, AnalyzableArticle)]
    assert len(passports) == 1


@pytest.mark.asyncio
async def test_doi_url_used_as_source_url() -> None:
    items = await _collect(_build(_items()).fetch(source_id=1))
    assert items
    for item in items:
        assert isinstance(item, AnalyzableArticle)
        assert str(item.source_url).startswith("https://doi.org/10.3390/")


@pytest.mark.asyncio
async def test_non_journal_article_type_dropped() -> None:
    item = _valid_item()
    item["type"] = "correction"
    items = await _collect(_build([item]).fetch(source_id=1))
    assert items == []


@pytest.mark.asyncio
async def test_non_cc_by_license_dropped() -> None:
    item = _valid_item()
    item["license"] = [{"URL": "https://creativecommons.org/licenses/by-nc/4.0/"}]
    items = await _collect(_build([item]).fetch(source_id=1))
    assert items == []


@pytest.mark.asyncio
async def test_missing_license_dropped() -> None:
    item = _valid_item()
    del item["license"]
    items = await _collect(_build([item]).fetch(source_id=1))
    assert items == []


@pytest.mark.asyncio
async def test_missing_title_dropped() -> None:
    item = _valid_item()
    item["title"] = []
    items = await _collect(_build([item]).fetch(source_id=1))
    assert items == []


@pytest.mark.asyncio
async def test_short_abstract_dropped() -> None:
    item = _valid_item()
    item["abstract"] = "<jats:p>too short</jats:p>"
    items = await _collect(_build([item]).fetch(source_id=1))
    assert items == []


@pytest.mark.asyncio
async def test_missing_date_parts_dropped() -> None:
    item = _valid_item()
    for key in ("published", "issued", "published-online", "published-print"):
        item.pop(key, None)
    items = await _collect(_build([item]).fetch(source_id=1))
    assert items == []


@pytest.mark.asyncio
async def test_missing_doi_dropped() -> None:
    item = _valid_item()
    del item["DOI"]
    items = await _collect(_build([item]).fetch(source_id=1))
    assert items == []


@pytest.mark.asyncio
async def test_client_kwargs_carry_issn_lookback_rows() -> None:
    fake = _FakeCrossrefClient([])
    await _collect(_fetcher(fake).fetch(source_id=1))
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["source_name"] == "MDPI Materials"
    assert call["issn"] == _MATERIALS_ISSN
    assert call["rows"] == _DEFAULT_ROWS_PER_REQUEST
    # from_pub_date は date.isoformat() 由来の "YYYY-MM-DD" 文字列
    assert isinstance(call["from_pub_date"], str)
    assert len(call["from_pub_date"]) == 10


@pytest.mark.asyncio
async def test_non_recoverable_error_propagates_through_collect() -> None:
    fetcher = _fetcher(
        _RaisingCrossrefClient(
            FetchAccessDeniedError(status_code=403, reason="forbidden")
        )
    )
    with pytest.raises(FetchAccessDeniedError):
        await _collect(fetcher.fetch(source_id=1))


@pytest.mark.asyncio
async def test_recoverable_error_propagates_through_collect() -> None:
    fetcher = _fetcher(
        _RaisingCrossrefClient(
            FetchOriginServerError(status_code=500, reason="internal_error")
        )
    )
    with pytest.raises(FetchOriginServerError):
        await _collect(fetcher.fetch(source_id=1))
