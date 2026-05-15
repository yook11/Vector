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
from typing import ClassVar

from app.collection.article.domain.article import ReadyForArticle
from app.collection.fetchers.tools.passport_builder import try_build_passport
from app.collection.fetchers.tools.rss_parser import RssEntry, RssParser
from app.collection.incomplete_article.domain.incomplete_article import (
    IncompleteArticle,
)

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

    async def fetch(
        self, source_id: int
    ) -> AsyncIterator[ReadyForArticle | IncompleteArticle]:
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
    ) -> ReadyForArticle | IncompleteArticle | None:
        return try_build_passport(
            title=entry.title,
            link=entry.link,
            body_candidate=_strip_html(_pick_body(entry)) or None,
            published_hint=entry.published,
            source_id=source_id,
        )
