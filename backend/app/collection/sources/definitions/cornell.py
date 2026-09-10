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


class CornellChronicleSource:
    """Cornell Chronicle の複数 feed Source。"""

    name: ClassVar[SourceName] = SourceName("Cornell Chronicle")
    acquisition: ClassVar[RssAcquisition] = RssAcquisition(
        feeds=CORNELL_FEEDS,
        parse_mode="bytes",
        body_policy=RssBodyPolicy.DISCARD,
    )
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY
    fetch_cadence: ClassVar[FetchCadence] = FetchCadence.MEDIUM

    @staticmethod
    def select(entries: list[RssEntry]) -> list[RssEntry]:
        """非空URLの初出を残し、空URLは後段の棄却監査へ渡す。"""
        return dedup_by_link(entries)
