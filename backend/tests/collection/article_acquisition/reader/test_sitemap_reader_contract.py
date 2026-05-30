"""Sitemap Reader 契約テスト (録画実 sitemap × 性質)。

``SitemapReader.fetch`` を公開面から検証する。差し替えるのは HTTP transport
のみで parse は本物が動く。固定するのは互いに別物の退行クラス:

- no-drop: loc 欠落 ``<url>`` も drop せず Entry にする (count parity)
- スコープ素通し: ``/news/`` 判定を Reader に持ち込まない (Source の責務)
- 構造的非記事: ``<sitemapindex>`` は Entry にしない
- R4: HTTP status / 不正 XML 全体失敗のみ typed error
- XXE: 外部実体を解決しない (parse 所有の帰結)

count parity は標本由来件数と比較 (literal 直書きせず録り直し自己追従)。
``anthropic_sitemap.xml`` は loc 欠落 ``<url>`` を 1 件含む (no-drop 検証を
空虚にしないための意図的標本 — provenance 規律)。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from lxml import etree

from app.collection.article_acquisition.reader.read_errors import (
    UnreadableResponseError,
    UnreadableResponseReason,
)
from app.collection.article_acquisition.reader.sitemap_reader import (
    SitemapEntry,
    SitemapReader,
)
from app.collection.external_fetch_errors import (
    FetchAccessDeniedError,
    FetchOriginServerError,
)

# reader/ -> fetchers/ -> collection/ -> tests/ -> tests/fixtures (C1 と同一)
_FIXTURES_DIR = Path(__file__).parents[3] / "fixtures"
# SitemapReader は RawHttpClient を wrap するため transport seam は
# raw_http_client モジュールに在る (普遍オラクルと同じ patch 対象)。
_MOD = "app.collection.article_acquisition.tools.raw_http_client"
_FIXTURE = "anthropic_sitemap.xml"
_SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
_URL = "https://www.anthropic.com/sitemap.xml"


def _raw_url_count() -> int:
    """録画 sitemap の ``<url>`` 要素数 (count parity の期待値を標本から導出)。"""
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    root = etree.fromstring((_FIXTURES_DIR / _FIXTURE).read_bytes(), parser=parser)
    return len(root.findall(f"{{{_SITEMAP_NS}}}url"))


def _response(status_code: int, content: bytes) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=content,
        request=httpx.Request("GET", _URL),
    )


async def _reader_entries(content: bytes) -> list[SitemapEntry]:
    """``SitemapReader().fetch`` を録画実バイトで走らせる (transport のみ fake)。"""
    response = _response(200, content)

    @asynccontextmanager
    async def _fake_safe_client(**_: Any) -> AsyncIterator[Any]:
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=response)
        yield client

    with patch(f"{_MOD}.make_safe_async_client", _fake_safe_client):
        return await SitemapReader().fetch(
            url=_URL, source_name="sitemap-reader-contract"
        )


async def test_reader_drops_no_recorded_url() -> None:
    """no-drop: 出力件数 == 録画 ``<url>`` 件数。loc 欠落も素通し。

    present-witness ``any(e.loc == "")`` が無いと現標本で count parity は
    空虚 (loc 欠落 ``<url>`` が標本に在り Reader 出力に現れることを固定)。
    """
    entries = await _reader_entries((_FIXTURES_DIR / _FIXTURE).read_bytes())
    assert len(entries) == _raw_url_count()
    assert any(e.loc == "" for e in entries), [e.loc for e in entries]


async def test_reader_does_not_apply_source_collection_scope() -> None:
    """スコープ素通し: ``/news/`` 配下も非 ``/news/`` も両方 Reader を通る
    (``/news/`` 判定は Source の責務であり Reader に持ち込まない)。"""
    entries = await _reader_entries((_FIXTURES_DIR / _FIXTURE).read_bytes())
    locs = [e.loc for e in entries]
    assert any("/news/" in loc for loc in locs), locs
    assert any(loc and "/news/" not in loc for loc in locs), locs


async def test_reader_emits_no_entry_for_structural_non_article() -> None:
    """構造的非記事: ``<sitemapindex>`` には ``SitemapEntry`` を作らない
    (値欠落 ``<url>`` は通すが ``<url>`` でない構造は通さない)。"""
    sitemapindex = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://www.anthropic.com/sitemap-news.xml</loc></sitemap>
  <sitemap><loc>https://www.anthropic.com/sitemap-docs.xml</loc></sitemap>
</sitemapindex>
"""
    entries = await _reader_entries(sitemapindex)
    assert entries == []


async def test_lastmod_is_tz_aware_or_none() -> None:
    """lastmod は tz-aware datetime か None (tz-naive を作らない)。"""
    entries = await _reader_entries((_FIXTURES_DIR / _FIXTURE).read_bytes())
    assert entries
    for e in entries:
        assert e.lastmod is None or e.lastmod.tzinfo is not None


async def _raise_through(status_code: int) -> None:
    response = _response(status_code, b"<urlset/>")

    @asynccontextmanager
    async def _fake_safe_client(**_: Any) -> AsyncIterator[Any]:
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=response)
        yield client

    with patch(f"{_MOD}.make_safe_async_client", _fake_safe_client):
        await SitemapReader().fetch(url=_URL, source_name="sitemap-reader-contract")


async def test_http_403_raises_access_denied() -> None:
    """R4: payload 全体失敗 (403) は ``ExternalFetchError`` 系に写る。"""
    with pytest.raises(FetchAccessDeniedError):
        await _raise_through(403)


async def test_http_500_raises_origin_server_error() -> None:
    """R4: payload 全体失敗 (500) は ``ExternalFetchError`` 系に写る。"""
    with pytest.raises(FetchOriginServerError):
        await _raise_through(500)


async def test_malformed_xml_raises_malformed_content() -> None:
    """構文破損 XML (非空) は ``MALFORMED_CONTENT`` に写り format / position で
    自己記述する (空 body との切り分けは下の empty-body テストが所有)。"""
    with pytest.raises(UnreadableResponseError) as raised:
        await _reader_entries(b"<urlset><url>")

    assert raised.value.reason is UnreadableResponseReason.MALFORMED_CONTENT
    assert raised.value.response_format == "xml"
    assert raised.value.parser_position is not None  # XMLSyntaxError 由来の line:col


async def test_empty_body_raises_empty_body() -> None:
    """空 body は parse 手前で ``EMPTY_BODY`` に倒す (空応答とブロックは別運用)。"""
    with pytest.raises(UnreadableResponseError) as raised:
        await _reader_entries(b"")

    assert raised.value.reason is UnreadableResponseReason.EMPTY_BODY
    assert raised.value.response_format == "xml"


async def test_xxe_external_entity_not_resolved() -> None:
    """XXE: 外部実体を解決しない (parse 所有の帰結 = Reader 契約)。

    旧 ``test_anthropic_adapter.py`` の ``_parse_sitemap`` 直叩きを parse の
    所在に追随させ Reader 公開面へ relocation。
    """
    malicious = b"""<?xml version="1.0"?>
<!DOCTYPE urlset [
  <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.anthropic.com/news/&xxe;</loc></url>
</urlset>
"""
    entries = await _reader_entries(malicious)
    assert entries  # 空 entries で trivial pass する空虚 edge を塞ぐ
    loc = entries[0].loc
    assert "/etc/passwd" not in loc
    assert "root:" not in loc
