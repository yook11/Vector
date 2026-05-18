"""ORNL (Oak Ridge National Laboratory) 用 Source — HTML listing Pattern H。

RSS / Atom / sitemap.xml を提供しないため、``/news`` listing ページから記事
URL を列挙する Pattern H 経路。listing には title が無いため URL slug を
プレースホルダとして ``title`` に詰め、仮タイトル性は per-source の補完方針
(``completion_profile = HTML_TITLE_PROFILE``、title=``html_preferred``) が
表す。parse helper (``_parse_listing`` / ``_slug_from_url``) は本モジュール
内に閉じる (Anthropic sitemap と問題が違うため共用しない)。

per-source 設計 (実 listing 観察ベース):

- listing URL: ``https://www.ornl.gov/news`` (200 OK、UTF-8、~64KB)
- detail link 抽出: ``//a[starts-with(@href, "/news/")]`` で 17 件取得
- category landing 除外: ``EXCLUDED_PATHS`` で path 単位の denylist
- robots.txt: /news/ 配下を許可、Crawl-delay 10s (host-level limiter は
  別レイヤ責務、collect 内で sleep しない)
- License: U.S. Government work、attribution_label = "ORNL · DOE"
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import ClassVar
from urllib.parse import urljoin, urlparse

from lxml import etree, html

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.domain.source_completion_profile import (
    HTML_TITLE_PROFILE,
    SourceCompletionProfile,
)
from app.collection.external_fetch_errors import FetchParseError
from app.collection.source_fetch.fetched_article import FetchedArticle
from app.collection.source_fetch.tools.fetch_tools import FetchTools
from app.shared.value_objects.source_name import SourceName


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


class ORNLSource:
    """ORNL news listing ``XxxSource`` (HTML listing, Pattern H)。

    ``published_at=None`` も intentional (listing は lastmod 情報を持たない
    前提、HTML 抽出側で確定させる)。

    business critical drop:
    - 同一 listing 内 URL dedup (同 href が複数 ``<a>`` で出る物理問題への対処)
    - ``EXCLUDED_PATHS`` denylist (category landing を弾く)
    - ``MAX_ENTRIES=30`` 切り出し (大量バックフィル防止)
    """

    name: ClassVar[SourceName] = SourceName("ORNL")
    endpoint_url: ClassVar[str] = "https://www.ornl.gov/news"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.listing
    completion_profile: ClassVar[SourceCompletionProfile] = HTML_TITLE_PROFILE

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

    @classmethod
    async def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        client = tools.raw_http(accept="text/html")
        listing_bytes = await client.fetch(
            url=cls.endpoint_url, source_name=str(cls.name)
        )
        try:
            urls = await asyncio.to_thread(
                _parse_listing,
                listing_bytes,
                detail_link_xpath=cls.DETAIL_LINK_XPATH,
                detail_url_prefix=cls.DETAIL_URL_PREFIX,
            )
        except etree.LxmlError as e:
            raise FetchParseError(f"html listing parse error: {cls.name}: {e}") from e

        seen: set[str] = set()
        emitted = 0
        for url in urls:
            if url in seen:
                continue
            seen.add(url)
            if urlparse(url).path in cls.EXCLUDED_PATHS:
                continue
            slug = _slug_from_url(url) or str(cls.name)
            yield FetchedArticle(
                title=slug,
                url=url,
                body=None,
                published_at=None,
            )
            emitted += 1
            if emitted >= cls.MAX_ENTRIES:
                break
