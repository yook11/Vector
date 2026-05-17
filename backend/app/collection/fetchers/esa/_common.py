"""ESA Djangoplicity 規格 RSS の取得 machinery (P2)。

Djangoplicity の News module は ESA/Hubble / ESA/Webb / ESO / ALMA で広く
使われる科学広報 CMS。RSS 出力は構造的に同型:

- RSS 2.0 (UTF-8、CET/CEST timezone の pubDate)
- ``<item>`` は ``<title>`` (CDATA, "Photo Release:" / "Science Release:" 等
  の prefix を含む) / ``<link>`` (絶対 URL) / ``<guid>`` (link と同値) /
  ``<pubDate>`` (RFC 822 +0100/+0200) / ``<description>`` (HTML の lead
  paragraph、~500-900 chars)
- ``<author>`` / ``<dc:creator>`` / ``<media:*>`` は **未提供**
- 本文は HTML 詳細ページに委譲 (Pattern H)

P1 までは継承基底で subclass が ``NAME`` / ``ENDPOINT_URL`` ClassVar を
差し替える形だった。P2 で per-source 知識は
``ArticleSource`` 集約へ移し、本クラスは Source 定義 (``source_name`` /
``endpoint_url``) を ``__init__`` で受け取る汎用 machinery になった
(ESA/Hubble / ESA/Webb は ``ArticleSource`` の ``adapter_factory`` から本
machinery を構築する)。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssParser


class DjangoplicityAdapter:
    """ESA Djangoplicity News module の Pattern H 取得 machinery (P2)。

    判定順は旧 ``BaseDjangoplicityFetcher._convert_entry`` を踏襲: title 空
    のみ structural gate (URL canonical は ``passport_builder`` に委譲)。本文は
    HTML 詳細ページに委譲する Pattern H のため ``body=None`` で渡す。
    ``source_name`` / ``endpoint_url`` は ``ArticleSource.adapter_factory`` から
    受け取る (identity の出所は Source 集約)。
    """

    def __init__(
        self,
        *,
        source_name: str,
        endpoint_url: str,
        parser: RssParser | None = None,
    ) -> None:
        self._source_name = source_name
        self._endpoint_url = endpoint_url
        self._parser = parser or RssParser()

    async def collect(self) -> AsyncIterator[FetchedArticle]:
        entries = await self._parser.fetch(
            endpoint_url=self._endpoint_url,
            source_name=self._source_name,
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
