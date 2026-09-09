"""VentureBeatのRSS取得宣言。"""

from typing import ClassVar

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.sources.article_completion_policy import (
    DEFAULT_POLICY,
    ArticleCompletionPolicy,
)
from app.collection.sources.fetch_cadence import FetchCadence
from app.collection.sources.rss_acquisition import RssAcquisition, RssBodyPolicy
from app.collection.sources.source_name import SourceName


class VentureBeatSource:
    name: ClassVar[SourceName] = SourceName("VentureBeat")
    acquisition: ClassVar[RssAcquisition] = RssAcquisition(
        feeds=("https://venturebeat.com/feed",),
        parse_mode="text",
        body_policy=RssBodyPolicy.LONGEST_CONTENT_OR_SUMMARY,
    )
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY
    fetch_cadence: ClassVar[FetchCadence] = FetchCadence.HIGH
