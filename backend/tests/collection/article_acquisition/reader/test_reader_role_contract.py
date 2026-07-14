"""Reader 役割契約の横断テスト。

録画した実 transport を Reader entrypoint に渡すと、機構固有の frozen
dataclass Entry 列が返ることを確認する。生 transport 型 (dict / bytes / str)
を Source に漏らさないことが主眼。

本ファイルは R1/R5 の形だけを見る。no-drop や typed-error 境界は機構別契約
テストで扱う。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.collection.article_acquisition.reader.algolia_hn_reader import HackerNewsReader
from app.collection.article_acquisition.reader.crossref_reader import CrossrefReader
from app.collection.article_acquisition.reader.html_listing_reader import (
    HtmlListingReader,
)
from app.collection.article_acquisition.reader.rss_reader import RssReader
from app.collection.article_acquisition.reader.sitemap_reader import SitemapReader

# reader/ -> fetchers/ -> collection/ -> tests/ -> tests/fixtures (C1 と同一)
_FIXTURES_DIR = Path(__file__).parents[3] / "fixtures"

_URL = "https://example.com/feed"
_NAME = "reader-role-contract"


@dataclass(frozen=True)
class _Mechanism:
    """1 機構分の fixture、patch 対象 module、Reader 呼び出し。"""

    name: str
    module: str  # make_safe_async_client を import している = patch 対象
    fixture: str  # 録画した実 transport バイト列
    invoke: Callable[[], Awaitable[object]]


_MECHANISMS: list[_Mechanism] = [
    _Mechanism(
        name="rss",
        module="app.collection.article_acquisition.reader.rss_reader",
        fixture="nist_rss.xml",
        invoke=lambda: RssReader().fetch(
            endpoint_url=_URL, source_name=_NAME, parse_mode="bytes"
        ),
    ),
    _Mechanism(
        name="hacker_news",
        module="app.collection.article_acquisition.reader.algolia_hn_reader",
        fixture="hacker_news_hits.json",
        invoke=lambda: HackerNewsReader().search_recent_stories(
            source_name=_NAME,
            min_points=0,
            window_seconds=10**12,
            hits_per_page=100,
        ),
    ),
    _Mechanism(
        name="crossref",
        module="app.collection.article_acquisition.reader.crossref_reader",
        fixture="mdpi_crossref.json",
        invoke=lambda: CrossrefReader(
            contact_email="crossref-contact@example.invalid"
        ).fetch_works(
            source_name=_NAME,
            issn="0000-0000",
            from_pub_date="2000-01-01",
            rows=100,
        ),
    ),
    _Mechanism(
        # SitemapReader は RawHttpClient を包むため patch 対象は raw_http_client。
        name="raw_sitemap",
        module="app.collection.article_acquisition.tools.raw_http_client",
        fixture="anthropic_sitemap.xml",
        invoke=lambda: SitemapReader().fetch(url=_URL, source_name=_NAME),
    ),
    _Mechanism(
        name="raw_html_listing",
        module="app.collection.article_acquisition.tools.raw_http_client",
        fixture="ornl_listing.html",
        # detail_link_xpath は Source 宣言値。fixture は ORNL の実 listing
        # なのでこの値で抽出する (HN min_points 等と同じ機構別 invoke 引数)。
        invoke=lambda: HtmlListingReader().fetch(
            url=_URL,
            source_name=_NAME,
            detail_link_xpath='//a[starts-with(@href, "/news/")]',
        ),
    ),
]

# 全機構 Reader 実体化済みのため既知 xfail はない。
_NOT_YET_A_READER: set[str] = set()


def _params() -> list[Any]:
    out: list[Any] = []
    for m in _MECHANISMS:
        marks = (
            pytest.mark.xfail(
                reason=(
                    f"{m.name}: Reader 未実体化 (生 transport 型を Source に漏らす)。"
                    " strangler で typed Entry 化し invoke を向け直したら marks を外す"
                ),
                strict=True,
            )
            if m.name in _NOT_YET_A_READER
            else ()
        )
        out.append(pytest.param(m, marks=marks, id=m.name))
    return out


async def _run(m: _Mechanism) -> object:
    """録画実 transport を当該機構の Reader 候補 entrypoint に流す。

    差し替えるのは ``make_safe_async_client`` のみ。HTTP status / json /
    bytes 取り出し / parse は機構実装の本物が動く。
    """
    raw = (_FIXTURES_DIR / m.fixture).read_bytes()
    response = httpx.Response(
        status_code=200,
        content=raw,
        request=httpx.Request("GET", _URL),
    )

    @asynccontextmanager
    async def _fake_safe_client(**_: Any) -> AsyncIterator[Any]:
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=response)
        yield client

    with patch(f"{m.module}.make_safe_async_client", _fake_safe_client):
        return await m.invoke()


@pytest.mark.parametrize("m", _params())
async def test_reader_returns_mechanism_typed_entry_boxes(m: _Mechanism) -> None:
    """Reader は録画実 transport から機構固有 typed Entry 箱の列を返す。

    R1: 単一の生 transport 塊でなく entry の列。
    R5: 各 entry は機構固有の frozen dataclass 箱 (生 transport 型 dict/bytes/
        str でない / enum でない)。
    """
    result = await _run(m)

    # R1: Reader は entry の列を返す (bytes 1 塊や生 dict 列でない)
    assert isinstance(result, list), (m.name, type(result))
    assert result, m.name  # 録画標本は最低1件 (空シートベルト防止)
    for e in result:
        # R5: 機構固有の frozen dataclass 箱
        assert is_dataclass(e) and not isinstance(e, type), (m.name, type(e))
        assert not isinstance(e, dict | bytes | str), (m.name, type(e))
        assert not isinstance(e, Enum), (m.name, type(e))
        assert e.__dataclass_params__.frozen, (m.name, type(e))
