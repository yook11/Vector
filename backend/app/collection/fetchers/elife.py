"""eLife (latest articles RSS) 用 Fetcher。

per-source 設計: eLife は実 feed では ``content`` が空 / 欠落で
``summary`` (description) に abstract 全文を載せる。``content_encoded`` と
``summary`` の長い方を採用 (VB と同型)。``parse_mode="bytes"`` で
feedparser に encoding sniff を委ねる。
"""

from __future__ import annotations

import html
import re
from collections.abc import AsyncIterator

from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssEntry, RssParser

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


class ELifeAdapter:
    """eLife 用 SourceAdapter (Pattern R、body 信用)。"""

    NAME = "eLife"
    ENDPOINT_URL = "https://elifesciences.org/rss/recent.xml"

    def __init__(self, parser: RssParser | None = None) -> None:
        self._parser = parser or RssParser()

    async def collect(self) -> AsyncIterator[FetchedArticle]:
        entries = await self._parser.fetch(
            endpoint_url=self.ENDPOINT_URL,
            source_name=self.NAME,
            parse_mode="bytes",
        )
        for entry in entries:
            yield FetchedArticle(
                title=entry.title,
                url=entry.link,
                body=_strip_html(_pick_body(entry)) or None,
                published_at=entry.published,
            )
