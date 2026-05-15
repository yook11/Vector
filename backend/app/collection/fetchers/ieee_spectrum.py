"""IEEE Spectrum 用 Fetcher。

per-source 設計: IEEE では feed の ``<content:encoded>`` が空で
``<description>`` (= ``summary``) に full body が入る。body は
``summary`` 直取り (他の WordPress 系 fetcher と body picker が逆)。
"""

from __future__ import annotations

import html
import re
from collections.abc import AsyncIterator
from typing import ClassVar

from app.collection.article.domain.article import ReadyForArticle
from app.collection.fetchers.tools.fetched_article import FetchedArticle
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
    """``<description>`` を直取り (IEEE では content[0] が空)。"""
    return entry.summary or ""


class IEEESpectrumFetcher:
    NAME: ClassVar[str] = "IEEE Spectrum"
    ENDPOINT_URL: ClassVar[str] = "https://spectrum.ieee.org/feeds/feed.rss"

    def __init__(self, parser: RssParser | None = None) -> None:
        self._parser = parser or RssParser()

    async def fetch(
        self, source_id: int
    ) -> AsyncIterator[ReadyForArticle | IncompleteArticle]:
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
    ) -> ReadyForArticle | IncompleteArticle | None:
        return try_build_passport(
            title=entry.title,
            link=entry.link,
            body_candidate=_strip_html(_pick_body(entry)) or None,
            published_hint=entry.published,
            source_id=source_id,
        )


class IEEESpectrumAdapter:
    """IEEE Spectrum 用 SourceAdapter (Pattern R、body 信用)。"""

    NAME = "IEEE Spectrum"
    ENDPOINT_URL = "https://spectrum.ieee.org/feeds/feed.rss"

    def __init__(self, parser: RssParser | None = None) -> None:
        self._parser = parser or RssParser()

    async def collect(self) -> AsyncIterator[FetchedArticle]:
        entries = await self._parser.fetch(
            endpoint_url=self.ENDPOINT_URL,
            source_name=self.NAME,
            parse_mode="text",
        )
        for entry in entries:
            yield FetchedArticle(
                title=entry.title,
                url=entry.link,
                body=_strip_html(_pick_body(entry)) or None,
                published_at=entry.published,
            )
