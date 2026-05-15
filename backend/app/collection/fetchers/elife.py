"""eLife (latest articles RSS) 用 Fetcher — Pattern R (RSS-only)。

per-source 設計: eLife は実 feed では ``content`` が空 / 欠落で
``summary`` (description) に abstract 全文を載せる。``content_encoded`` と
``summary`` の長い方を採用 (VB と同型)。``parse_mode="bytes"`` で
feedparser に encoding sniff を委ねる。
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
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _pick_body(entry: RssEntry) -> str:
    """``<content:encoded>`` と ``<description>`` の長い方を採用 (VB と同型)。"""
    content_encoded = entry.content_encoded or ""
    summary = entry.summary or ""
    return content_encoded if len(content_encoded) >= len(summary) else summary


class ELifeFetcher:
    NAME: ClassVar[str] = "eLife"
    ENDPOINT_URL: ClassVar[str] = "https://elifesciences.org/rss/recent.xml"

    def __init__(self, parser: RssParser | None = None) -> None:
        self._parser = parser or RssParser()

    async def fetch(self, source_id: int) -> AsyncIterator[ReadyForArticle]:
        entries = await self._parser.fetch(
            endpoint_url=self.ENDPOINT_URL,
            source_name=self.NAME,
            parse_mode="bytes",
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
