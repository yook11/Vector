"""sitemap.xml の Reader (HTTP 取得 + ``<url>`` → Entry 抽出)。

defensive parsing (``resolve_entities=False`` / ``no_network=True`` /
``load_dtd=False``) で XXE / 外部 DTD 読込を塞ぐ。``<url>`` を 1 件ずつ
``SitemapEntry`` にし、loc 欠落・lastmod 不正は drop せず素通しする
(収集スコープ判定・空 URL の棄却は後段の責務)。``<sitemapindex>`` は
``<url>`` を持たないため Entry を 1 つも作らない (構造的非記事)。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from lxml import etree

from app.collection.article_acquisition.errors import UnreadableResponseError
from app.collection.article_acquisition.tools.raw_http_client import RawHttpClient

_SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"


@dataclass(frozen=True, slots=True)
class SitemapEntry:
    """sitemap の 1 ``<url>`` を写した Entry (記述用・invariant 無し)。

    ``loc`` は ``<loc>`` 欠落時 ``""`` (drop せず素通し)。``lastmod`` は
    parse 不能なら ``None``。意味づけ (記事か / スコープ内か) は後段。
    """

    loc: str
    lastmod: datetime | None


def _parse_lastmod(text: str | None) -> datetime | None:
    """``<lastmod>`` を tz-aware datetime に。parse 不能は ``None``。"""
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _parse_sitemap_entries(data: bytes) -> list[SitemapEntry]:
    """``<urlset>`` の各 ``<url>`` を ``SitemapEntry`` に写す (no-drop)。

    ``<sitemapindex>`` は ``<url>`` を持たず空列を返す (構造的非記事)。
    """
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    root = etree.fromstring(data, parser=parser)
    ns = {"s": _SITEMAP_NS}
    entries: list[SitemapEntry] = []
    for url_elem in root.findall("s:url", ns):
        loc_elem = url_elem.find("s:loc", ns)
        loc = loc_elem.text.strip() if loc_elem is not None and loc_elem.text else ""
        lastmod_elem = url_elem.find("s:lastmod", ns)
        lastmod = _parse_lastmod(
            lastmod_elem.text if lastmod_elem is not None else None
        )
        entries.append(SitemapEntry(loc=loc, lastmod=lastmod))
    return entries


class SitemapReader:
    """sitemap.xml Reader。transport は ``RawHttpClient`` を wrap する。"""

    def __init__(self, *, http: RawHttpClient | None = None) -> None:
        self._http = (
            http if http is not None else RawHttpClient(accept="application/xml")
        )

    async def fetch(self, *, url: str, source_name: str) -> list[SitemapEntry]:
        """HTTP GET → defensive XML parse → ``list[SitemapEntry]``。

        Raises:
            UnreadableResponseError: XML 構造破損 (payload 全体の失敗)。
            ExternalFetchError: HTTP status / transport / SSRF 例外の写像。
        """
        raw = await self._http.fetch(url=url, source_name=source_name)
        try:
            return await asyncio.to_thread(_parse_sitemap_entries, raw)
        except etree.XMLSyntaxError as e:
            raise UnreadableResponseError(
                f"sitemap parse error: {source_name}: {e}"
            ) from e
