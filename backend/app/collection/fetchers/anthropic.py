"""Anthropic 用 Source — sitemap-only Pattern H。

Anthropic は ``/rss.xml`` ``/feed`` ``/news/rss.xml`` 全て 404 で RSS を一切
提供せず、唯一 ``/sitemap.xml`` のみが利用可能。sitemap には title が無いため
URL slug をプレースホルダとして ``title`` に詰め、仮タイトル性は per-source の
補完方針 (``completion_profile = HTML_TITLE_PROFILE``、title=``html_preferred``)
が表す。sitemap parse helper (``_parse_sitemap`` / ``_slug_from_url``) は本
モジュール内に閉じる (ORNL listing と問題が違うため共用しない)。

attribution は Anthropic 公式の標準利用規約相当文言が無いため、source name
``"Anthropic"`` を ``news_sources.attribution_label`` に詰める (DB 行は
alembic ``o3_add_anthropic`` で seed)。

robots.txt: ``User-agent: *`` で ``Allow: /`` blanket + ``Sitemap:`` 明示。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import ClassVar
from urllib.parse import urlparse

from lxml import etree

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.domain.source_completion_profile import (
    HTML_TITLE_PROFILE,
    SourceCompletionProfile,
)
from app.collection.external_fetch_errors import FetchParseError
from app.collection.fetchers.tools.fetch_tools import FetchTools
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.shared.value_objects.source_name import SourceName

_SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"


def _parse_sitemap(data: bytes) -> list[tuple[str, datetime | None]]:
    """``<urlset>`` から ``(loc, lastmod)`` のタプル列を抽出する。

    defensive parsing: ``resolve_entities=False`` + ``no_network=True`` +
    ``load_dtd=False`` で XXE / 外部 DTD 読込を構造的に塞ぐ (defusedxml 不要)。
    lastmod parse 失敗は ``None`` に落とす (entry 自体は drop しない)。
    """
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
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


def _slug_from_url(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1]


class AnthropicSource:
    """Anthropic news の sitemap-only ``XxxSource`` (Pattern H)。

    business critical drop:
    - ``URL_PATH_PREFIX="/news/"`` で news セクション以外を弾く
      (about / pricing 等の混入防止)
    - lastmod 降順 sort 後に ``MAX_ENTRIES=30`` で切り出し (大量バックフィル防止)
    """

    name: ClassVar[SourceName] = SourceName("Anthropic")
    endpoint_url: ClassVar[str] = "https://www.anthropic.com/sitemap.xml"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.sitemap
    completion_profile: ClassVar[SourceCompletionProfile] = HTML_TITLE_PROFILE

    URL_PATH_PREFIX: ClassVar[str] = "/news/"
    MAX_ENTRIES: ClassVar[int] = 30

    @classmethod
    async def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        client = tools.raw_http(accept="application/xml")
        sitemap_bytes = await client.fetch(
            url=cls.endpoint_url, source_name=str(cls.name)
        )
        try:
            entries = await asyncio.to_thread(_parse_sitemap, sitemap_bytes)
        except etree.XMLSyntaxError as e:
            raise FetchParseError(f"sitemap parse error: {cls.name}: {e}") from e

        filtered = [
            (loc, lastmod)
            for loc, lastmod in entries
            if urlparse(loc).path.startswith(cls.URL_PATH_PREFIX)
        ]
        _epoch = datetime.min.replace(tzinfo=UTC)
        filtered.sort(key=lambda e: e[1] or _epoch, reverse=True)

        for loc, lastmod in filtered[: cls.MAX_ENTRIES]:
            slug = _slug_from_url(loc) or str(cls.name)
            yield FetchedArticle(
                title=slug,
                url=loc,
                body=None,
                published_at=lastmod,
            )
