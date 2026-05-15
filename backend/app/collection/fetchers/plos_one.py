"""PLOS ONE 用 Fetcher (Atom 1.0)。

per-source 設計: Atom 1.0 仕様で ``<content type="html">`` に abstract 本文
(1.4K-3K chars 平均) を含む、Tier 1 ソース中で唯一の Atom feed。``content``
を一級採用、欠落時のみ ``summary`` (= Atom ``<summary>``) に fallback する。
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
    """Atom ``<content>`` を優先、欠落時のみ ``<summary>`` に fallback する。"""
    if entry.content_encoded:
        return entry.content_encoded
    return entry.summary or ""


class PLOSOneFetcher:
    NAME: ClassVar[str] = "PLOS ONE"
    ENDPOINT_URL: ClassVar[str] = "https://journals.plos.org/plosone/feed/atom"

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
