"""IEEE Spectrum 用 Fetcher。

per-source 設計: IEEE では feed の ``<content:encoded>`` が空で
``<description>`` (= ``summary``) に full body が入る。body は
``summary`` 直取り (他の WordPress 系 fetcher と body picker が逆)。
"""

from __future__ import annotations

import html
import re
from collections.abc import AsyncIterator

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.domain.source_completion_profile import DEFAULT_PROFILE
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssEntry, RssParser

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_html(s: str) -> str:
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _pick_body(entry: RssEntry) -> str:
    """``<description>`` を直取り (IEEE では content[0] が空)。"""
    return entry.summary or ""


class IEEESpectrumAdapter:
    """IEEE Spectrum 用 SourceAdapter (Pattern R、body 信用)。"""

    NAME = "IEEE Spectrum"
    ENDPOINT_URL = "https://spectrum.ieee.org/feeds/feed.rss"
    observed_origin = ObservedOrigin.feed
    completion_profile = DEFAULT_PROFILE

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
