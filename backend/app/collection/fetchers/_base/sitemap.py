"""sitemap.xml 経路の Pattern H Fetcher 基底 — Phase 3 PR 3-d-4 で新設。

RSS / Atom / RDF を一切提供しないソース (Anthropic news が初導入) を取り
込むため、``sitemap.xml`` の ``<urlset>`` から URL を列挙し各 URL を
``IncompleteArticle`` として yield する。後段の ``extract_html_body`` task
が trafilatura で本文 + title を抽出して merge → ``ReadyForArticle`` 構築。

sitemap.xml は title を一切含まないため、本基底は ``IncompleteArticle.title``
に **URL slug をプレースホルダ** として詰め、``prefer_html_title=True`` で
HTML 抽出由来の title を採用するよう merge 規則に opt-in する。HTML 抽出が
失敗した場合は記事ごと drop されるためプレースホルダは永続化されない。

将来再利用想定: HuggingFace / Meta AI の RSS が落ちた場合の sitemap fallback。
共通化で earnest な投資として基底クラスを最小限に保ち、固有挙動は subclass
の ClassVar で表現する (``URL_PATH_PREFIX`` / ``MAX_ENTRIES`` / ``LANGUAGE``
等)。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import ClassVar
from urllib.parse import urlparse

import httpx
import structlog
from lxml import etree

from app.collection.article.domain.value_objects import PublishedAt
from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.incomplete_article.domain.incomplete_article import (
    IncompleteArticle,
)
from app.shared.security.safe_http import make_safe_async_client
from app.shared.security.ssrf_guard import HostBlockedError, HostResolutionError
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl

logger = structlog.get_logger(__name__)

_USER_AGENT = "Mozilla/5.0 (compatible; Vector/1.0; +https://github.com/yook11/Vector)"
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
_SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"


class BaseSitemapFetcher:
    """sitemap.xml 経路の Pattern H Fetcher 基底。

    subclass は以下の ClassVar を必ず宣言する:

    - ``NAME``: ``news_sources.name`` と一致する dispatch キー
    - ``ENDPOINT_URL``: sitemap.xml の URL (``/sitemap.xml``)
    - ``URL_PATH_PREFIX``: 採用対象の URL path 接頭辞 (例: ``/news/``)

    オプショナル ClassVar (デフォルト値あり):

    - ``MAX_ENTRIES``: 1 cron 周期で yield する最大件数。lastmod 降順で上位
      を採用する。delta fetch 相当 (大量バックフィルを防止)

    本基底は ``Fetcher`` Protocol (``NAME`` / ``ENDPOINT_URL`` +
    ``async def fetch``) を満たすため、subclass がそのまま ``FETCHERS``
    dispatch dict に登録できる。
    """

    NAME: ClassVar[str]
    ENDPOINT_URL: ClassVar[str]
    URL_PATH_PREFIX: ClassVar[str]
    MAX_ENTRIES: ClassVar[int] = 30

    async def fetch(self, source_id: int) -> AsyncIterator[IncompleteArticle]:
        sitemap_bytes = await self._fetch_sitemap()
        try:
            entries = await asyncio.to_thread(self._parse_sitemap, sitemap_bytes)
        except etree.XMLSyntaxError as e:
            logger.warning("sitemap_parse_error", source=self.NAME, error=str(e))
            raise PermanentFetchError(f"sitemap parse error: {self.NAME}: {e}") from e

        filtered = [e for e in entries if self._url_matches(e[0])]
        _epoch = datetime.min.replace(tzinfo=UTC)
        filtered.sort(key=lambda e: e[1] or _epoch, reverse=True)

        for loc, lastmod in filtered[: self.MAX_ENTRIES]:
            item = self._convert_entry(loc, lastmod, source_id)
            if item is not None:
                yield item

    async def _fetch_sitemap(self) -> bytes:
        async with make_safe_async_client(
            headers={"User-Agent": _USER_AGENT, "Accept": "application/xml"},
            verify=True,
            timeout=_HTTP_TIMEOUT,
        ) as client:
            try:
                response = await client.get(self.ENDPOINT_URL)
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

    @staticmethod
    def _parse_sitemap(data: bytes) -> list[tuple[str, datetime | None]]:
        """``<urlset>`` から ``(loc, lastmod)`` のタプル列を抽出する。

        lxml の標準 ``etree.fromstring`` は外部実体参照 (XXE) と DTD 読込を
        既定で処理しないため (``resolve_entities=False`` 相当)、追加の
        defusedxml を入れずに defensive parsing が成立する。lastmod parse
        失敗は ``None`` に落とす (entry 自体は drop しない)。
        """
        parser = etree.XMLParser(
            resolve_entities=False, no_network=True, load_dtd=False
        )
        root = etree.fromstring(data, parser=parser)
        ns = {"s": _SITEMAP_NS}
        result: list[tuple[str, datetime | None]] = []
        for url_elem in root.findall("s:url", ns):
            loc_elem = url_elem.find("s:loc", ns)
            if loc_elem is None or not loc_elem.text:
                continue
            loc = loc_elem.text.strip()
            lastmod_elem = url_elem.find("s:lastmod", ns)
            lastmod: datetime | None = None
            if lastmod_elem is not None and lastmod_elem.text:
                try:
                    lastmod = datetime.fromisoformat(
                        lastmod_elem.text.strip().replace("Z", "+00:00")
                    )
                    if lastmod.tzinfo is None:
                        lastmod = lastmod.replace(tzinfo=UTC)
                except ValueError:
                    lastmod = None
            result.append((loc, lastmod))
        return result

    def _url_matches(self, url: str) -> bool:
        path = urlparse(url).path
        return path.startswith(self.URL_PATH_PREFIX)

    def _convert_entry(
        self,
        loc: str,
        lastmod: datetime | None,
        source_id: int,
    ) -> IncompleteArticle | None:
        """1 sitemap entry を ``IncompleteArticle`` に変換する純関数。

        title は URL slug をプレースホルダとして詰め、``prefer_html_title=True``
        で HTML 抽出 task が trafilatura 由来の title で overwrite する経路を
        選ぶ。lastmod は ``published_at_hint`` に流して、HTML 抽出が
        ``published_at`` を出さないケースの最後の砦にする。
        """
        try:
            source_url = CanonicalArticleUrl(loc)
        except ValueError:
            return None

        slug = self._slug_from_url(loc) or self.NAME
        title = slug[:500]
        published_hint = PublishedAt(value=lastmod) if lastmod is not None else None
        return IncompleteArticle(
            title=title,
            source_id=source_id,
            source_url=source_url,
            published_at_hint=published_hint,
            prefer_html_title=True,
        )

    @staticmethod
    def _slug_from_url(url: str) -> str:
        path = urlparse(url).path.rstrip("/")
        last = path.rsplit("/", 1)[-1]
        return last
