"""HTML listing 経路の Pattern H Fetcher 基底 — Phase 3 PR 3-i-1 で新設。

RSS / Atom / sitemap.xml を提供せず、HTML の listing ページから記事 URL を
列挙するソース (ORNL が初導入) を取り込むための基底。``BaseSitemapFetcher``
と同じ思想で、Stage A (listing GET + 記事 URL 列挙) のみを本基底の責務とし、
Stage B (HTML 本文抽出) は既存 ``extract_html_body`` task が担う。

設計判断:

- HTML parser は ``lxml.html`` を採用 (sitemap.py で既に lxml 直接依存、
  cssselect / selectolax の新規依存を増やさない)。link 抽出は XPath で行う
  (``cssselect`` は lxml 内蔵ではなく追加依存)。
- listing page はロケール別・カテゴリ別に分かれていることがあるため、
  ``LISTING_URL`` を ClassVar で 1 本指定する設計とする (複数 listing は
  ``FEEDS`` パターンの別基底で扱う、本基底は 1 listing 想定)。
- detail URL は relative の場合があるため ``DETAIL_URL_PREFIX`` を urljoin
  で適用する。``EXCLUDED_PATHS`` で listing ページ内の category landing リンク
  等を path 単位で除外できる (XPath で頑張らず Python 側で分かりやすく書く)。
- title は HTML 抽出 task の責務 (``prefer_html_title=True`` で merge 規則を
  HTML 由来採用に opt-in)。本基底では URL slug をプレースホルダとして詰める
  (HTML 抽出失敗時はその記事ごと drop されるためプレースホルダは永続化されない)。
- crawl-delay は本基底の ``fetch()`` 内で sleep しない。yield した
  ``IncompleteArticle`` は taskiq 経由で別 worker が分散処理するため
  in-process sleep は queue rate と無関係。host-level rate limiter は
  ``extract_html_body`` task 側の将来 PR で対応する (TODO)。

将来再利用想定: SemiWiki / 各種 HTML 専用ニュースサイト。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import ClassVar
from urllib.parse import urljoin, urlparse

import httpx
import structlog
from lxml import etree, html

from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.incomplete_article.domain.incomplete_article import (
    IncompleteArticle,
)
from app.shared.security.safe_http import make_safe_async_client
from app.shared.security.ssrf_guard import HostBlockedError, HostResolutionError
from app.shared.value_objects.safe_url import SafeUrl

logger = structlog.get_logger(__name__)

_USER_AGENT = "Mozilla/5.0 (compatible; Vector/1.0; +https://github.com/yook11/Vector)"
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)


class BaseHtmlListingFetcher:
    """HTML listing 経路の Pattern H Fetcher 基底。

    subclass は以下の ClassVar を必ず宣言する:

    - ``NAME``: ``news_sources.name`` と一致する dispatch キー
    - ``ENDPOINT_URL``: listing page URL (``LISTING_URL`` と同値で OK)
    - ``LISTING_URL``: listing HTML を GET する URL
    - ``DETAIL_LINK_XPATH``: 記事 detail への ``<a>`` を抽出する XPath
    - ``DETAIL_URL_PREFIX``: relative href を絶対 URL に解決する base
      (典型的には ``https://example.com``)

    オプショナル ClassVar (デフォルト値あり):

    - ``EXCLUDED_PATHS``: listing 内の category landing 等、記事ではない
      URL path を除外するための frozenset。XPath で除外を頑張らずに済む。
    - ``MAX_ENTRIES``: 1 cron 周期で yield する最大件数 (大量バックフィル防止)

    本基底は ``Fetcher`` Protocol (``NAME`` / ``ENDPOINT_URL`` +
    ``async def fetch``) を満たすため、subclass がそのまま ``FETCHERS``
    dispatch dict に登録できる。
    """

    NAME: ClassVar[str]
    ENDPOINT_URL: ClassVar[str]
    LISTING_URL: ClassVar[str]
    DETAIL_LINK_XPATH: ClassVar[str]
    DETAIL_URL_PREFIX: ClassVar[str]
    EXCLUDED_PATHS: ClassVar[frozenset[str]] = frozenset()
    MAX_ENTRIES: ClassVar[int] = 30

    async def fetch(self, source_id: int) -> AsyncIterator[IncompleteArticle]:
        listing_bytes = await self._fetch_listing()
        try:
            urls = await asyncio.to_thread(self._parse_listing, listing_bytes)
        except etree.LxmlError as e:
            logger.warning("html_listing_parse_error", source=self.NAME, error=str(e))
            raise PermanentFetchError(
                f"html listing parse error: {self.NAME}: {e}"
            ) from e

        seen: set[str] = set()
        emitted = 0
        for url in urls:
            if url in seen:
                continue
            seen.add(url)
            if not self._url_matches(url):
                continue
            item = self._convert_entry(url, source_id)
            if item is None:
                continue
            yield item
            emitted += 1
            if emitted >= self.MAX_ENTRIES:
                break

    async def _fetch_listing(self) -> bytes:
        async with make_safe_async_client(
            headers={"User-Agent": _USER_AGENT, "Accept": "text/html"},
            verify=True,
            timeout=_HTTP_TIMEOUT,
        ) as client:
            try:
                response = await client.get(self.LISTING_URL)
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status in (403, 404, 410, 451):
                    raise PermanentFetchError(f"HTTP {status}: {self.NAME}") from e
                raise TemporaryFetchError(f"HTTP {status}: {self.NAME}") from e
            except httpx.RequestError as e:
                raise TemporaryFetchError(f"request error: {self.NAME}: {e}") from e
            except HostBlockedError as e:
                raise PermanentFetchError(str(e)) from e
            except HostResolutionError as e:
                raise TemporaryFetchError(str(e)) from e
            return response.content

    @classmethod
    def _parse_listing(cls, data: bytes) -> list[str]:
        """listing HTML から ``DETAIL_LINK_XPATH`` で href を抽出し絶対 URL 化する。

        defensive parsing: ``lxml.html.fromstring`` は外部 entity 解決を行わず
        no_network parser を内部で使うため、追加の defusedxml は不要。
        """
        doc = html.fromstring(data)
        result: list[str] = []
        for elem in doc.xpath(cls.DETAIL_LINK_XPATH):
            href = elem.get("href") if hasattr(elem, "get") else None
            if not href:
                continue
            absolute = urljoin(cls.DETAIL_URL_PREFIX, href.strip())
            result.append(absolute)
        return result

    def _url_matches(self, url: str) -> bool:
        """``EXCLUDED_PATHS`` を ``urlparse(url).path`` 単位で除外する。

        subclass で更に厳密なフィルタ (slug pattern 等) が必要であれば
        override する。デフォルトは ``EXCLUDED_PATHS`` のみ。
        """
        path = urlparse(url).path
        return path not in self.EXCLUDED_PATHS

    def _convert_entry(self, loc: str, source_id: int) -> IncompleteArticle | None:
        """1 listing entry を ``IncompleteArticle`` に変換する純関数。

        title は URL slug をプレースホルダとして詰め、``prefer_html_title=True``
        で HTML 抽出 task が trafilatura 由来の title で overwrite する経路を
        選ぶ。listing page に lastmod 情報がない前提で
        ``published_at_hint=None`` を返し、HTML 抽出側で確定させる。
        """
        try:
            source_url = SafeUrl(loc)
        except ValueError:
            return None

        slug = self._slug_from_url(loc) or self.NAME
        title = slug[:500]
        return IncompleteArticle(
            title=title,
            source_id=source_id,
            source_url=source_url,
            published_at_hint=None,
            prefer_html_title=True,
        )

    @staticmethod
    def _slug_from_url(url: str) -> str:
        path = urlparse(url).path.rstrip("/")
        last = path.rsplit("/", 1)[-1]
        return last
