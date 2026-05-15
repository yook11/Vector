"""ESA Djangoplicity 規格 RSS Fetcher の共通基底。

Djangoplicity の News module は ESA/Hubble / ESA/Webb / ESO / ALMA で広く
使われる科学広報 CMS。RSS 出力は構造的に同型:

- RSS 2.0 (UTF-8、CET/CEST timezone の pubDate)
- ``<item>`` は ``<title>`` (CDATA, "Photo Release:" / "Science Release:" 等
  の prefix を含む) / ``<link>`` (絶対 URL) / ``<guid>`` (link と同値) /
  ``<pubDate>`` (RFC 822 +0100/+0200) / ``<description>`` (HTML の lead
  paragraph、~500-900 chars)
- ``<author>`` / ``<dc:creator>`` / ``<media:*>`` は **未提供**
- 本文は HTML 詳細ページに委譲 (Pattern H)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar

from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssParser


class BaseDjangoplicityAdapter:
    """ESA Djangoplicity News module の Pattern H SourceAdapter 共通基底。

    subclass は ``NAME`` / ``ENDPOINT_URL`` の 2 ClassVar を必須で差し替える
    (MDPI base+subclass と同形)。判定順は旧
    ``BaseDjangoplicityFetcher._convert_entry`` を踏襲: title 空のみ structural
    gate (URL canonical は ``passport_builder`` に委譲)。本文は HTML 詳細ページ
    に委譲する Pattern H のため ``body=None`` で渡す。
    """

    NAME: ClassVar[str]
    ENDPOINT_URL: ClassVar[str]

    def __init__(self, parser: RssParser | None = None) -> None:
        self._parser = parser or RssParser()

    async def collect(self) -> AsyncIterator[FetchedArticle]:
        entries = await self._parser.fetch(
            endpoint_url=self.ENDPOINT_URL,
            source_name=self.NAME,
            parse_mode="bytes",
        )
        for entry in entries:
            title = entry.title[:500]
            if not title:
                continue
            yield FetchedArticle(
                title=title,
                url=entry.link,
                body=None,
                published_at=entry.published,
            )
