"""PLOS ONE 用 Source (Atom 1.0)。

``<content type="html">`` に abstract 本文を含む。``content`` を優先し
欠落時のみ ``summary`` (= Atom ``<summary>``) に fallback する。
"""

from __future__ import annotations

import html
import re
from typing import ClassVar

from app.collection.article_acquisition.fetched_article import FetchedArticle
from app.collection.article_acquisition.reader.rss_reader import RssEntry
from app.collection.article_acquisition.tools.reader_tools import ReaderTools
from app.collection.domain.observed_article import ObservedOrigin
from app.collection.sources.article_completion_policy import (
    DEFAULT_POLICY,
    ArticleCompletionPolicy,
)
from app.collection.sources.base_article_source import BaseArticleSource
from app.collection.sources.fetch_cadence import FetchCadence
from app.collection.sources.source_name import SourceName

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


class PLOSOneSource(BaseArticleSource):
    """PLOS ONE 用 Source (Atom 1.0)。"""

    name: ClassVar[SourceName] = SourceName("PLOS ONE")
    endpoint_url: ClassVar[str] = "https://journals.plos.org/plosone/feed/atom"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY
    fetch_cadence: ClassVar[FetchCadence] = FetchCadence.LOW

    @classmethod
    async def read(cls, tools: ReaderTools) -> list[RssEntry]:
        return await tools.rss.fetch(
            endpoint_url=cls.endpoint_url,
            source_name=str(cls.name),
            parse_mode="bytes",
        )

    @classmethod
    def map_entry(cls, entry: RssEntry) -> FetchedArticle:
        return FetchedArticle(
            title=entry.title,
            url=entry.link,
            body=_strip_html(_pick_body(entry)) or None,
            published_at=entry.published,
        )
