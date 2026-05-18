"""NASA 用 Source (複数 feed)。

NASA は 6 feed の multi-feed RSS (本体 + news-release / technology /
aeronautics / station / artemis)。body は ``<content:encoded>`` を plain text
化して採用する (nav noise を含むまま後段 LLM 側で吸収)。per-feed 失敗隔離・
feed 横断 dedup は ``multi_feed_rss`` が担う。
"""

from __future__ import annotations

import html
import re
from collections.abc import AsyncIterator
from typing import ClassVar, Final

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.domain.source_completion_profile import (
    DEFAULT_PROFILE,
    SourceCompletionProfile,
)
from app.collection.source_fetch.fetched_article import FetchedArticle
from app.collection.source_fetch.tools.fetch_tools import FetchTools
from app.collection.source_fetch.tools.multi_feed_rss import multi_feed_rss
from app.collection.source_fetch.tools.rss_parser import RssEntry
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


class NASASource:
    """NASA news の複数 feed Source。"""

    name: ClassVar[SourceName] = SourceName("NASA")
    endpoint_url: ClassVar[str] = "https://www.nasa.gov/feed/"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_profile: ClassVar[SourceCompletionProfile] = DEFAULT_PROFILE

    @classmethod
    def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        return multi_feed_rss(
            tools,
            source_name=str(cls.name),
            feeds=NASA_FEEDS,
            parse_mode="text",
            body_builder=nasa_build_body,
        )
