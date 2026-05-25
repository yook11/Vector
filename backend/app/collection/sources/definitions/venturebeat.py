"""VentureBeat 用 Source。

VB の RSS feed は ``<description>`` / ``<content:encoded>`` に full body
(~12000 chars) を含む。WordPress VIP の truncate 差を吸収するため
``<content:encoded>`` と ``<description>`` の長い方を本文に採用する。
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
from app.shared.value_objects.source_name import SourceName

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_html(s: str) -> str:
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _pick_body(entry: RssEntry) -> str:
    """``content_encoded`` と ``summary`` の長い方を採用する (truncate 差吸収)。"""
    content_encoded = entry.content_encoded or ""
    summary = entry.summary or ""
    return content_encoded if len(content_encoded) >= len(summary) else summary


class VentureBeatSource(BaseArticleSource):
    """VentureBeat 用 Source。"""

    name: ClassVar[SourceName] = SourceName("VentureBeat")
    endpoint_url: ClassVar[str] = "https://venturebeat.com/feed"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY
    fetch_cadence: ClassVar[FetchCadence] = FetchCadence.HIGH

    @classmethod
    async def read(cls, tools: ReaderTools) -> list[RssEntry]:
        return await tools.rss.fetch(
            endpoint_url=cls.endpoint_url,
            source_name=str(cls.name),
            parse_mode="text",
        )

    @classmethod
    def map_entry(cls, entry: RssEntry) -> FetchedArticle:
        """WordPress VIP の truncate 差吸収のため長い方を本文に採る。"""
        return FetchedArticle(
            title=entry.title,
            url=entry.link,
            body=_strip_html(_pick_body(entry)) or None,
            published_at=entry.published,
        )
