"""ORNL (Oak Ridge National Laboratory) 用 Fetcher / Adapter — HTML listing Pattern H。

RSS / Atom / sitemap.xml を提供しないため、``/news`` listing ページから記事
URL を列挙する Pattern H 経路。

旧 ``ORNLNewsFetcher`` は ``BaseHtmlListingFetcher`` を継承していたが、P5 で
``SourceAdapter`` 化するに際し、本基底のサブクラスは ORNL 1 本のみで共用が
成立しない (``feedback_no_share_different_problems``) ため、新 ``ORNLAdapter``
は standalone とし、parse helper (``_parse_listing`` / ``_slug_from_url``)
を本モジュール内に最小限再実装する。

旧 ``ORNLNewsFetcher`` / ``BaseHtmlListingFetcher`` は P6 strategy 切替 +
P7 cleanup まで無変更で残置 (Strangler 移行)。

per-source 設計 (実 listing 観察ベース):

- listing URL: ``https://www.ornl.gov/news`` (200 OK、UTF-8、~64KB)
- detail link 抽出: ``//a[starts-with(@href, "/news/")]`` で 17 件取得
- category landing 除外: ``EXCLUDED_PATHS`` で path 単位の denylist
- robots.txt: /news/ 配下を許可、Crawl-delay 10s (host-level limiter は
  別レイヤ責務、Adapter 内で sleep しない)
- License: U.S. Government work、attribution_label = "ORNL · DOE"
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import ClassVar
from urllib.parse import urljoin, urlparse

from lxml import etree, html

from app.collection.external_fetch_errors import FetchParseError
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.raw_http_client import RawHttpClient


def _parse_listing(
    data: bytes,
    *,
    detail_link_xpath: str,
    detail_url_prefix: str,
) -> list[str]:
    """listing HTML から XPath で href を抽出し絶対 URL 化する。

    defensive parsing: ``lxml.html.fromstring`` は外部 entity を解決せず
    no_network parser を内部で使うため、defusedxml は不要。
    """
    doc = html.fromstring(data)
    result: list[str] = []
    for elem in doc.xpath(detail_link_xpath):
        href = elem.get("href") if hasattr(elem, "get") else None
        if not href:
            continue
        absolute = urljoin(detail_url_prefix, href.strip())
        result.append(absolute)
    return result


def _slug_from_url(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1]


class ORNLAdapter:
    """ORNL news listing ``SourceAdapter`` (HTML listing, Pattern H)。

    listing HTML は title を持たないため、Adapter は URL slug をプレースホルダ
    として ``title`` に詰め、``prefer_html_title=True`` で HTML 補完経路を
    強制する。``published_at=None`` も intentional (listing は lastmod 情報を
    持たない前提、HTML 抽出側で確定させる)。

    business critical drop:
    - 同一 listing 内 URL dedup (同 href が複数 ``<a>`` で出る物理問題への対処)
    - ``EXCLUDED_PATHS`` denylist (category landing を弾く)
    - ``MAX_ENTRIES=30`` 切り出し (大量バックフィル防止)
    """

    NAME: ClassVar[str] = "ORNL"
    ENDPOINT_URL: ClassVar[str] = "https://www.ornl.gov/news"
    LISTING_URL: ClassVar[str] = "https://www.ornl.gov/news"
    DETAIL_LINK_XPATH: ClassVar[str] = '//a[starts-with(@href, "/news/")]'
    DETAIL_URL_PREFIX: ClassVar[str] = "https://www.ornl.gov"
    # 2026-05-04 時点の実 listing で確認した category landing 6 件。
    EXCLUDED_PATHS: ClassVar[frozenset[str]] = frozenset(
        {
            "/news/releases",
            "/news/features",
            "/news/researcher-profiles",
            "/news/story-tips",
            "/news/audio-spots",
            "/news/honors-and-awards",
        }
    )
    MAX_ENTRIES: ClassVar[int] = 30

    def __init__(self, client: RawHttpClient | None = None) -> None:
        self._client = client or RawHttpClient(accept="text/html")

    async def collect(self) -> AsyncIterator[FetchedArticle]:
        listing_bytes = await self._client.fetch(
            url=self.LISTING_URL, source_name=self.NAME
        )
        try:
            urls = await asyncio.to_thread(
                _parse_listing,
                listing_bytes,
                detail_link_xpath=self.DETAIL_LINK_XPATH,
                detail_url_prefix=self.DETAIL_URL_PREFIX,
            )
        except etree.LxmlError as e:
            raise FetchParseError(f"html listing parse error: {self.NAME}: {e}") from e

        seen: set[str] = set()
        emitted = 0
        for url in urls:
            if url in seen:
                continue
            seen.add(url)
            if urlparse(url).path in self.EXCLUDED_PATHS:
                continue
            slug = _slug_from_url(url) or self.NAME
            yield FetchedArticle(
                title=slug,
                url=url,
                body=None,
                published_at=None,
                prefer_html_title=True,
            )
            emitted += 1
            if emitted >= self.MAX_ENTRIES:
                break
