"""Cornell Chronicle 用 Source (複数 feed)。

Cornell Chronicle (``https://news.cornell.edu/``) は学部別の
``/taxonomy/term/<id>/feed`` でカテゴリ別 RSS を提供する。本体 ``/news/feed``
は site-wide で雑多なため採用せず、対象 6 カテゴリのみを巡回する。feed は
Drupal 生成 RSS 2.0。description は短い概要のみで本文は HTML 取得に委ねる。
1 記事が複数 category に tag されるため feed 間で URL 重複が起きる
(per-feed 失敗隔離は ``MultiFeedRssReader``、横断 dedup は ``select`` が担う)。
"""

from __future__ import annotations

from typing import ClassVar, Final

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
from app.collection.sources.rss_dedup import dedup_by_link
from app.shared.value_objects.source_name import SourceName

CORNELL_FEEDS: Final[tuple[str, ...]] = (
    # Artificial Intelligence
    "https://news.cornell.edu/taxonomy/term/24043/feed",
    # Computing & Information Sciences
    "https://news.cornell.edu/taxonomy/term/14256/feed",
    # Life Sciences & Veterinary Medicine
    "https://news.cornell.edu/taxonomy/term/15056/feed",
    # Energy, Environment & Sustainability
    "https://news.cornell.edu/taxonomy/term/15621/feed",
    # Physical Sciences & Engineering
    "https://news.cornell.edu/taxonomy/term/14252/feed",
    # Health, Nutrition & Medicine
    "https://news.cornell.edu/taxonomy/term/14248/feed",
)


class CornellChronicleSource(BaseArticleSource):
    """Cornell Chronicle の複数 feed Source。"""

    name: ClassVar[SourceName] = SourceName("Cornell Chronicle")
    endpoint_url: ClassVar[str] = "https://news.cornell.edu/taxonomy/term/24043/feed"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY
    fetch_cadence: ClassVar[FetchCadence] = FetchCadence.MEDIUM

    @classmethod
    async def read(cls, tools: ReaderTools) -> list[RssEntry]:
        return await tools.multi_feed_rss().fetch(
            source_name=str(cls.name),
            feeds=CORNELL_FEEDS,
            parse_mode="bytes",
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
            body=None,
            published_at=entry.published,
        )
