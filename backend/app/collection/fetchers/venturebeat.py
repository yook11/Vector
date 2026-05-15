"""VentureBeat 用 Fetcher — Pattern R (RSS-only) の参照実装。

per-source 設計: VB の RSS feed は ``<description>`` / ``<content:encoded>``
に full body (~12000 chars) を含み、HTML 取得を経由せずに本文を構築できる
(`spec collection-source-rss-research.md`)。これにより VB が時折 Vercel
Challenge で 5xx を返す問題を構造的に回避する。

HTTP 取得 / feedparser / SSRF guard / title plain text 正規化は L2
``RssParser`` に集約済。本ファイルは L3 翻訳 (RssEntry → ReadyForArticle)
の per-source 責務だけを持つ。body picker は WordPress VIP の truncate 差を
吸収するため ``<content:encoded>`` と ``<description>`` の長い方を採用する。
"""

from __future__ import annotations

import html
import re
from collections.abc import AsyncIterator
from typing import ClassVar

from app.collection.article.domain.article import ReadyForArticle
from app.collection.article.domain.value_objects import PublishedAt
from app.collection.fetchers.tools.rss_parser import RssEntry, RssParser
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_html(s: str) -> str:
    """HTML tag 除去 + entity decode + 空白圧縮。body の plain text 化に使う。"""
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _pick_body(entry: RssEntry) -> str:
    """``content_encoded`` と ``summary`` の長い方を本文として採用する。

    一部 WordPress VIP サイト (VB / IEEE Spectrum 等) では片方が truncate
    される運用上の差分があり、長い方を採用するロジックで吸収する
    (`spec collection-source-rss-research.md` の「max(content_encoded, summary)」)。
    """
    content_encoded = entry.content_encoded or ""
    summary = entry.summary or ""
    return content_encoded if len(content_encoded) >= len(summary) else summary


class VentureBeatFetcher:
    """VentureBeat 用 RSS-only Fetcher。"""

    NAME: ClassVar[str] = "VentureBeat"
    ENDPOINT_URL: ClassVar[str] = "https://venturebeat.com/feed"

    def __init__(self, parser: RssParser | None = None) -> None:
        self._parser = parser or RssParser()

    async def fetch(self, source_id: int) -> AsyncIterator[ReadyForArticle]:
        entries = await self._parser.fetch(
            endpoint_url=self.ENDPOINT_URL,
            source_name=self.NAME,
            parse_mode="text",
        )
        for entry in entries:
            item = self._convert_entry(entry, source_id)
            if item is not None:
                yield item

    def _convert_entry(
        self,
        entry: RssEntry,
        source_id: int,
    ) -> ReadyForArticle | None:
        """1 entry を ``ReadyForArticle`` に変換する。"""
        title = entry.title[:500]
        if not title:
            return None

        body = _strip_html(_pick_body(entry))
        if len(body) < 50:
            return None

        if entry.published is None:
            return None

        try:
            source_url = CanonicalArticleUrl(entry.link)
        except ValueError:
            return None

        try:
            return ReadyForArticle(
                title=title,
                body=body,
                published_at=PublishedAt(value=entry.published),
                source_id=source_id,
                source_url=source_url,
            )
        except ValueError:
            return None
