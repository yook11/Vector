"""複数フィードの取得宣言と候補選択。"""

from __future__ import annotations

from typing import ClassVar, Final

from app.collection.article_acquisition.reader.rss_reader import RssEntry
from app.collection.domain.observed_article import ObservedOrigin
from app.collection.sources.article_completion_policy import (
    DEFAULT_POLICY,
    ArticleCompletionPolicy,
)
from app.collection.sources.fetch_cadence import FetchCadence
from app.collection.sources.rss_acquisition import RssAcquisition, RssBodyPolicy
from app.collection.sources.rss_dedup import dedup_by_link
from app.collection.sources.source_name import SourceName

NASA_FEEDS: Final[tuple[str, ...]] = (
    "https://www.nasa.gov/feed/",
    "https://www.nasa.gov/news-release/feed/",
    "https://www.nasa.gov/technology/feed/",
    "https://www.nasa.gov/aeronautics/feed/",
    "https://www.nasa.gov/missions/station/feed/",
    "https://www.nasa.gov/missions/artemis/feed/",
)


class NASASource:
    """NASA news の複数 feed Source。"""

    name: ClassVar[SourceName] = SourceName("NASA")
    acquisition: ClassVar[RssAcquisition] = RssAcquisition(
        feeds=NASA_FEEDS,
        parse_mode="text",
        body_policy=RssBodyPolicy.CONTENT_ENCODED,
    )
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY
    fetch_cadence: ClassVar[FetchCadence] = FetchCadence.MEDIUM

    @staticmethod
    def select(entries: list[RssEntry]) -> list[RssEntry]:
        """非空URLの初出を残し、空URLは後段の棄却監査へ渡す。"""
        return dedup_by_link(entries)
