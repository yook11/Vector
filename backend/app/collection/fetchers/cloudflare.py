"""Cloudflare Blog 用 Fetcher。

per-source 設計: RSS feed の ``<content:encoded>`` に full body が含まれる。
body は ``content_encoded`` を直取り。``parse_mode="bytes"`` で feedparser
に encoding sniff を委ねる。
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
    """``<content:encoded>`` を直取り。"""
    return entry.content_encoded or ""


class CloudflareBlogAdapter:
    """The Cloudflare Blog 用 SourceAdapter (Pattern R、body 信用)。"""

    NAME = "The Cloudflare Blog"
    ENDPOINT_URL = "https://blog.cloudflare.com/rss/"
    observed_origin = ObservedOrigin.feed
    completion_profile = DEFAULT_PROFILE

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
