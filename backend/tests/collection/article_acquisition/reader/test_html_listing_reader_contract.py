"""HTML listing Reader 契約テスト (録画実 listing × 性質)。

``HtmlListingReader.fetch`` を公開面から検証する。差し替えるのは HTTP
transport のみで parse は本物が動く。固定するのは互いに別物の退行クラス:

- no-drop: dedup を Reader に持ち込まない (重複 href も素通し)
- xpath 抽出: detail link xpath に一致する ``<a>`` のみ抽出
- R4: HTTP status 全体失敗のみ typed error
- XXE: 外部実体を解決しない (hardened parser・sitemap と対称)

count parity は標本由来の xpath 一致 ``<a>`` 件数と比較。``ornl_listing.html``
は同一 href の重複を含む (dedup を Reader へ漏らす退行を空虚にしない
provenance)。dedup / EXCLUDED_PATHS / cap は後段 Source の責務。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from lxml import html

from app.collection.article_acquisition.reader.html_listing_reader import (
    HtmlListingEntry,
    HtmlListingReader,
)
from app.collection.external_fetch_errors import (
    FetchAccessDeniedError,
    FetchOriginServerError,
)

# reader/ -> fetchers/ -> collection/ -> tests/ -> tests/fixtures (C1 と同一)
_FIXTURES_DIR = Path(__file__).parents[3] / "fixtures"
# HtmlListingReader は RawHttpClient を wrap するため transport seam は
# raw_http_client モジュールに在る (普遍オラクルと同じ patch 対象)。
_MOD = "app.collection.article_acquisition.tools.raw_http_client"
_FIXTURE = "ornl_listing.html"
_URL = "https://www.ornl.gov/news"
# ORNL listing の detail link 抽出 xpath (Source 宣言値。fixture は ORNL の
# 実 listing なのでこの値で抽出する provenance)。
_XPATH = '//a[starts-with(@href, "/news/")]'


def _raw_match_count() -> int:
    """録画 listing の xpath 一致 ``<a>`` 件数 (count parity の期待値)。"""
    doc = html.fromstring((_FIXTURES_DIR / _FIXTURE).read_bytes())
    return len(doc.xpath(_XPATH))


def _response(status_code: int, content: bytes) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=content,
        request=httpx.Request("GET", _URL),
    )


async def _reader_entries(content: bytes) -> list[HtmlListingEntry]:
    """``HtmlListingReader().fetch`` を録画実バイトで走らせる (transport のみ fake)。"""
    response = _response(200, content)

    @asynccontextmanager
    async def _fake_safe_client(**_: Any) -> AsyncIterator[Any]:
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=response)
        yield client

    with patch(f"{_MOD}.make_safe_async_client", _fake_safe_client):
        return await HtmlListingReader().fetch(
            url=_URL,
            source_name="html-listing-reader-contract",
            detail_link_xpath=_XPATH,
        )


async def test_reader_drops_no_recorded_link() -> None:
    """no-drop: 出力件数 == 録画 xpath 一致 ``<a>`` 件数。dedup は持ち込まない。

    present-witness (重複 href が出力に残る) が無いと count parity は空虚
    (Source の dedup を Reader へ漏らす退行を検出する標本性質を固定)。
    """
    entries = await _reader_entries((_FIXTURES_DIR / _FIXTURE).read_bytes())
    assert len(entries) == _raw_match_count()
    assert len(entries) > len({e.href for e in entries}), [e.href for e in entries]


async def test_reader_extracts_only_xpath_matching_links() -> None:
    """xpath 抽出: 抽出 href は全て detail link xpath (``/news/``) に一致。"""
    entries = await _reader_entries((_FIXTURES_DIR / _FIXTURE).read_bytes())
    assert entries
    for e in entries:
        assert e.href.startswith("/news/"), e.href


async def _raise_through(status_code: int) -> None:
    response = _response(status_code, b"<html></html>")

    @asynccontextmanager
    async def _fake_safe_client(**_: Any) -> AsyncIterator[Any]:
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=response)
        yield client

    with patch(f"{_MOD}.make_safe_async_client", _fake_safe_client):
        await HtmlListingReader().fetch(
            url=_URL,
            source_name="html-listing-reader-contract",
            detail_link_xpath=_XPATH,
        )


async def test_http_403_raises_access_denied() -> None:
    """R4: payload 全体失敗 (403) は ``ExternalFetchError`` 系に写る。"""
    with pytest.raises(FetchAccessDeniedError):
        await _raise_through(403)


async def test_http_500_raises_origin_server_error() -> None:
    """R4: payload 全体失敗 (500) は ``ExternalFetchError`` 系に写る。"""
    with pytest.raises(FetchOriginServerError):
        await _raise_through(500)


async def test_xxe_external_entity_not_resolved() -> None:
    """XXE: 外部実体を解決しない (hardened parser の帰結 = Reader 契約)。

    HTML listing 経路には元来 XXE テストが無かった (sitemap のみ)。parse を
    Reader へ移し明示 hardened parser 化する今、sitemap と対称に pin する。
    """
    malicious = b"""<?xml version="1.0"?>
<!DOCTYPE html [
  <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<html><body>
  <a href="/news/&xxe;">leak</a>
</body></html>
"""
    entries = await _reader_entries(malicious)
    for e in entries:
        assert "/etc/passwd" not in e.href
        assert "root:" not in e.href
