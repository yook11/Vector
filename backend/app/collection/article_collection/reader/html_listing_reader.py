"""HTML listing ページの Reader (HTTP 取得 + detail link → Entry 抽出)。

listing には記事の普遍構造が無いため、どの ``<a>`` が記事候補かは Source が
xpath で宣言し Reader へ渡す (``detail_link_xpath``)。Reader は一致した
``<a>`` の href を 1 件ずつ ``HtmlListingEntry`` にする。dedup / 絶対 URL 化 /
EXCLUDED_PATHS 除外は持ち込まない (後段 Source の責務)。defensive parsing は
``no_network=True`` の明示 hardened ``HTMLParser`` で行う (外部実体・network
fetch を構造的に塞ぐ。sitemap Reader の hardened ``XMLParser`` と対称)。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from lxml import etree, html

from app.collection.article_collection.tools.raw_http_client import RawHttpClient
from app.collection.external_fetch_errors import FetchParseError

# defensive parsing: network fetch / 外部 entity を構造的に塞ぐ hardened parser。
_HTML_PARSER = html.HTMLParser(no_network=True)


@dataclass(frozen=True, slots=True)
class HtmlListingEntry:
    """listing の 1 detail link を写した Entry (記述用・invariant 無し)。

    ``href`` は ``<a>`` の生の href 文字列 (相対のまま)。絶対 URL 化は
    Source の純写像の責務 (spec: 相対→URL 組立は Source mapping)。
    """

    href: str


def _parse_listing_entries(
    data: bytes, *, detail_link_xpath: str
) -> list[HtmlListingEntry]:
    """xpath に一致する ``<a>`` の href を ``HtmlListingEntry`` に写す (no-drop)。

    dedup しない (同一 href の重複もそのまま通す — 重複排除は Source)。
    """
    doc = html.fromstring(data, parser=_HTML_PARSER)
    entries: list[HtmlListingEntry] = []
    for elem in doc.xpath(detail_link_xpath):
        href = elem.get("href") if hasattr(elem, "get") else None
        entries.append(HtmlListingEntry(href=(href or "").strip()))
    return entries


class HtmlListingReader:
    """HTML listing Reader。transport は ``RawHttpClient`` を wrap する。"""

    def __init__(self, *, http: RawHttpClient | None = None) -> None:
        self._http = http if http is not None else RawHttpClient(accept="text/html")

    async def fetch(
        self, *, url: str, source_name: str, detail_link_xpath: str
    ) -> list[HtmlListingEntry]:
        """HTTP GET → defensive HTML parse → ``list[HtmlListingEntry]``。

        Raises:
            FetchParseError: HTML 構造破損 (payload 全体の失敗)。
            ExternalFetchError: HTTP status / transport / SSRF 例外の写像。
        """
        raw = await self._http.fetch(url=url, source_name=source_name)
        try:
            return await asyncio.to_thread(
                _parse_listing_entries, raw, detail_link_xpath=detail_link_xpath
            )
        except etree.LxmlError as e:
            raise FetchParseError(
                f"html listing parse error: {source_name}: {e}"
            ) from e
