"""NASA 用 Source (複数 feed)。

NASA は 6 feed の multi-feed RSS (本体 + news-release / technology /
aeronautics / station / artemis)。body は ``<content:encoded>`` を plain text
化して採用する (nav noise を含むまま後段 LLM 側で吸収)。per-feed 失敗隔離は
``MultiFeedRssReader`` (``read``)、feed 横断 dedup は ``select`` が担う。
"""

from __future__ import annotations

import html
import re
from typing import ClassVar, Final

from app.collection.article_collection.fetched_article import FetchedArticle
from app.collection.article_collection.reader.rss_reader import RssEntry
from app.collection.article_collection.tools.reader_tools import ReaderTools
from app.collection.domain.observed_article import ObservedOrigin
from app.collection.sources.article_completion_policy import (
    DEFAULT_POLICY,
    ArticleCompletionPolicy,
)
from app.collection.sources.base_article_source import BaseArticleSource
from app.collection.sources.rss_dedup import dedup_by_link
from app.shared.value_objects.source_name import SourceName

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")

NASA_FEEDS: Final[tuple[str, ...]] = (
    "https://www.nasa.gov/feed/",
    "https://www.nasa.gov/news-release/feed/",
    "https://www.nasa.gov/technology/feed/",
    "https://www.nasa.gov/aeronautics/feed/",
    "https://www.nasa.gov/missions/station/feed/",
    "https://www.nasa.gov/missions/artemis/feed/",
)


def _strip_html(s: str) -> str:
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def nasa_build_body(entry: RssEntry) -> str | None:
    return _strip_html(entry.content_encoded or "") or None


class NASASource(BaseArticleSource):
    """NASA news の複数 feed Source。"""

    name: ClassVar[SourceName] = SourceName("NASA")
    endpoint_url: ClassVar[str] = "https://www.nasa.gov/feed/"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY

    @classmethod
    async def read(cls, tools: ReaderTools) -> list[RssEntry]:
        return await tools.multi_feed_rss().fetch(
            source_name=str(cls.name),
            feeds=NASA_FEEDS,
            parse_mode="text",
        )

    @classmethod
    def select(cls, entries: list[RssEntry]) -> list[RssEntry]:
        """feed 横断 URL dedup (空 link は除外せず素通し)。"""
        return dedup_by_link(entries)

    @classmethod
    def map_entry(cls, entry: RssEntry) -> FetchedArticle:
        return FetchedArticle(
            title=entry.title,
            url=entry.link,
            body=nasa_build_body(entry),
            published_at=entry.published,
        )
