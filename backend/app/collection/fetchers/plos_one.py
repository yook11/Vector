"""PLOS ONE 用 Fetcher (Atom 1.0)。

per-source 設計: Atom 1.0 仕様で ``<content type="html">`` に abstract 本文
(1.4K-3K chars 平均) を含む、Tier 1 ソース中で唯一の Atom feed。``content``
を一級採用、欠落時のみ ``summary`` (= Atom ``<summary>``) に fallback する。
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
    """Atom ``<content>`` を優先、欠落時のみ ``<summary>`` に fallback する。"""
    if entry.content_encoded:
        return entry.content_encoded
    return entry.summary or ""


class PLOSOneAdapter:
    """PLOS ONE 用 SourceAdapter (Atom 1.0、Pattern R、body 信用)。"""

    def __init__(
        self,
        *,
        endpoint_url: str,
        source_name: str,
        parser: RssParser | None = None,
    ) -> None:
        self._endpoint_url = endpoint_url
        self._source_name = source_name
        self._parser = parser or RssParser()

    async def collect(self) -> AsyncIterator[FetchedArticle]:
        entries = await self._parser.fetch(
            endpoint_url=self._endpoint_url,
            source_name=self._source_name,
            parse_mode="bytes",
        )
        for entry in entries:
            yield FetchedArticle(
                title=entry.title,
                url=entry.link,
                body=_strip_html(_pick_body(entry)) or None,
                published_at=entry.published,
            )
